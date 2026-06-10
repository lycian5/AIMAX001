import os
import re
import requests
from flask import Flask, request, jsonify, render_template
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

ARTICLE_SYSTEM_PROMPT = """당신은 한국 인터넷 신문사의 전문 기자 AI입니다.

기사 작성 규칙:
1. 제공된 소재를 바탕으로 최소 2000자 이상 기사를 작성하세요.
2. 일반 신문 기사 형식을 유지하세요 (제목, 본문, 출처 순서).
3. 팩트 중심으로 작성하고 추측이나 의견은 명확히 구분하세요.
4. 독자의 이해를 돕기 위해 배경 설명과 구체적 수치를 포함하세요.
5. 기사 말미에 출처 URL을 반드시 명시하세요.

출력 형식:
[제목]
(기사 제목)

[기사 본문]
(2000자 이상의 기사 내용)

[출처]
- (출처명): (URL)"""


def get_openai_client():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY 환경변수가 설정되지 않았습니다.")
    return OpenAI(api_key=key)


def clean_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def extract_youtube_channel(query):
    """유튜브 채널 URL이면 (handle, keyword) 반환, 아니면 (None, query)"""
    match = re.search(r"youtube\.com/(?:@|c/|channel/)([^/?&\s]+)", query)
    if not match:
        return None, query
    handle = match.group(1)
    keyword = re.sub(r"https?://\S+", "", query).strip()
    return handle, keyword


def get_youtube_channel_id(handle):
    """채널 핸들에서 채널 ID 조회"""
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={
            "key": os.environ.get("YOUTUBE_API_KEY", ""),
            "forHandle": handle,
            "part": "id",
        },
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return items[0]["id"] if items else None


def normalize_query(query):
    if query.startswith("http"):
        return re.sub(r"https?://\S+", "", query).strip()
    return query


def search_naver_news(query):
    resp = requests.get(
        "https://openapi.naver.com/v1/search/news.json",
        headers={
            "X-Naver-Client-Id": os.environ.get("NAVER_CLIENT_ID", ""),
            "X-Naver-Client-Secret": os.environ.get("NAVER_CLIENT_SECRET", ""),
        },
        params={"query": query, "display": 7, "sort": "date"},
        timeout=10,
    )
    resp.raise_for_status()
    results = []
    for item in resp.json().get("items", []):
        results.append({
            "title": clean_html(item.get("title", "")),
            "summary": clean_html(item.get("description", "")),
            "url": item.get("originallink") or item.get("link", ""),
            "source_type": "news",
            "source_name": item.get("name", "네이버 뉴스"),
        })
    return results


def search_youtube(query, channel_id=None):
    params = {
        "key": os.environ.get("YOUTUBE_API_KEY", ""),
        "part": "snippet",
        "type": "video",
        "maxResults": 4,
        "relevanceLanguage": "ko",
        "order": "date",
    }
    if query:
        params["q"] = query
    if channel_id:
        params["channelId"] = channel_id
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    results = []
    for item in resp.json().get("items", []):
        snippet = item.get("snippet", {})
        video_id = item.get("id", {}).get("videoId", "")
        results.append({
            "title": snippet.get("title", ""),
            "summary": snippet.get("description", "")[:200],
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "source_type": "youtube",
            "source_name": snippet.get("channelTitle", "YouTube"),
        })
    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json()
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "검색어를 입력해주세요."}), 400

    yt_handle, yt_keyword = extract_youtube_channel(query)
    search_keyword = normalize_query(query)
    materials = []
    errors = []

    # 유튜브 채널 URL인 경우: 채널 내부에서만 검색
    if yt_handle:
        try:
            channel_id = get_youtube_channel_id(yt_handle)
            if not channel_id:
                errors.append(f"채널을 찾을 수 없습니다: {yt_handle}")
            else:
                materials.extend(search_youtube(yt_keyword, channel_id=channel_id))
        except Exception as e:
            errors.append(f"유튜브 채널 오류: {str(e)}")
    else:
        try:
            materials.extend(search_naver_news(search_keyword))
        except Exception as e:
            errors.append(f"네이버 뉴스 오류: {str(e)}")

        try:
            materials.extend(search_youtube(search_keyword))
        except Exception as e:
            errors.append(f"유튜브 오류: {str(e)}")

    if not materials:
        return jsonify({"error": "검색 결과가 없습니다. " + " / ".join(errors)}), 404

    return jsonify({"materials": materials})


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json()
    material_title   = data.get("material_title", "").strip()
    material_summary = data.get("material_summary", "").strip()
    material_url     = data.get("material_url", "").strip()
    source_type      = data.get("source_type", "news")
    source_name      = data.get("source_name", "")

    if not material_title:
        return jsonify({"error": "소재를 선택해주세요."}), 400

    source_label = "유튜브 영상" if source_type == "youtube" else "뉴스 기사"

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": ARTICLE_SYSTEM_PROMPT},
                {"role": "user", "content": f"""다음 {source_label} 소재를 바탕으로 2000자 이상의 팩트 중심 기사를 작성해주세요.

선택된 소재:
- 제목: {material_title}
- 내용 요약: {material_summary}
- 출처: {source_name} ({source_label})
- URL: {material_url}

위 소재를 깊이 있게 분석하고 배경 정보와 의미를 포함하여 2000자 이상의 완성도 높은 기사를 작성해주세요."""},
            ],
            max_tokens=4096,
        )
        article_text = response.choices[0].message.content or ""
        return jsonify({"article": article_text, "sources": [material_url]})

    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"기사 생성 중 오류: {str(e)}"}), 500


if __name__ == "__main__":
    missing = [k for k in ["OPENAI_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "YOUTUBE_API_KEY"]
               if not os.environ.get(k)]
    if missing:
        print(f"경고: .env 파일에 다음 키를 설정해주세요: {', '.join(missing)}")
    app.run(debug=True, port=5000)
