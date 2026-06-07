import os
import json
import re
from flask import Flask, request, jsonify, render_template
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

SEARCH_SYSTEM_PROMPT = """당신은 한국 인터넷 신문사의 취재 보조 AI입니다.
사용자가 입력한 키워드를 바탕으로 Google 검색을 통해 정부 공식 사이트나 공공기관 자료에서 기사로 쓸 수 있는 소재를 5~7개 찾아주세요.

반드시 아래 JSON 형식만 반환하세요. 설명이나 다른 텍스트는 절대 포함하지 마세요:
{
  "materials": [
    {
      "title": "기사 제목 후보 (구체적이고 명확하게)",
      "summary": "소재 요약 (2~3문장, 핵심 팩트 포함)",
      "url": "출처 URL (정부 공식 사이트 우선)"
    }
  ]
}"""

ARTICLE_SYSTEM_PROMPT = """당신은 한국 인터넷 신문사의 전문 기자 AI입니다.

기사 작성 규칙:
1. 제공된 소재와 출처를 바탕으로 추가 웹 검색을 통해 관련 정보를 더 수집하세요.
2. 기사는 최소 2000자 이상 작성하세요.
3. 일반 신문 기사 형식을 유지하세요 (제목, 본문, 출처 순서).
4. 팩트 중심으로 작성하고 추측이나 의견은 명확히 구분하세요.
5. 기사 말미에 참고한 출처(URL 포함)를 반드시 명시하세요.

출력 형식:
[제목]
(기사 제목)

[기사 본문]
(2000자 이상의 기사 내용)

[출처]
- (출처명): (URL)"""


def extract_json(text):
    """응답 텍스트에서 JSON 블록 추출"""
    if not text:
        raise ValueError("응답이 비어 있습니다.")
    # 직접 파싱 시도
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # ```json ... ``` 코드 블록 추출
    match = re.search(r"```(?:json)?\s*(\{.*?})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # 첫 번째 { 부터 마지막 } 까지 추출
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        return json.loads(text[start:end])
    raise ValueError("JSON을 찾을 수 없습니다.")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json()
    query = data.get("query", "").strip()

    if not query:
        return jsonify({"error": "검색어를 입력해주세요."}), 400

    try:
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=f"다음 키워드와 관련된 기사 소재를 정부 공식 자료에서 5~7개 찾아 JSON으로 반환해주세요: {query}",
            config=types.GenerateContentConfig(
                system_instruction=SEARCH_SYSTEM_PROMPT,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )

        result = extract_json(response.text)
        materials = result.get("materials", [])

        if not materials:
            return jsonify({"error": "관련 소재를 찾지 못했습니다. 다른 키워드로 시도해주세요."}), 404

        return jsonify({"materials": materials})

    except (json.JSONDecodeError, ValueError):
        return jsonify({"error": "소재 목록을 파싱하는 데 실패했습니다. 다시 시도해주세요."}), 500
    except Exception as e:
        return jsonify({"error": f"소재 검색 중 오류: {str(e)}"}), 500


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json()
    material_title = data.get("material_title", "").strip()
    material_summary = data.get("material_summary", "").strip()
    material_url = data.get("material_url", "").strip()

    if not material_title:
        return jsonify({"error": "소재를 선택해주세요."}), 400

    try:
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=f"""다음 소재를 바탕으로 2000자 이상의 팩트 중심 기사를 작성해주세요.

선택된 소재:
- 제목: {material_title}
- 요약: {material_summary}
- 참고 출처: {material_url}

추가 웹 검색으로 더 많은 관련 정보를 수집하여 상세한 기사를 작성해주세요. 출처 링크를 반드시 포함하세요.""",
            config=types.GenerateContentConfig(
                system_instruction=ARTICLE_SYSTEM_PROMPT,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )

        article_text = response.text or ""

        sources = []
        candidate = response.candidates[0] if response.candidates else None
        if candidate and candidate.grounding_metadata:
            for chunk in candidate.grounding_metadata.grounding_chunks or []:
                if chunk.web and chunk.web.uri and chunk.web.uri not in sources:
                    sources.append(chunk.web.uri)

        return jsonify({"article": article_text, "sources": sources})

    except Exception as e:
        return jsonify({"error": f"기사 생성 중 오류: {str(e)}"}), 500


if __name__ == "__main__":
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or api_key == "여기에_API_키_입력":
        print("경고: .env 파일에 GEMINI_API_KEY를 설정해주세요.")
    app.run(debug=True, port=5000)
