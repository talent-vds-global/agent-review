from google import genai
from google.genai import types
from google.genai import errors
import time
import config
from context.tools import query_rules

# Danh mục các công cụ hỗ trợ cho agent
AVAILABLE_TOOLS = {
    "query_rules": query_rules,
}


def _call_model_with_retry(client, contents, config_obj, max_retries=5):
    """Gọi Gemini có retry với exponential backoff.

    Chỉ thử lại với lỗi tạm thời từ phía server (503 quá tải, 429 quá giới hạn tốc độ).
    Các lỗi khác (sai key, sai tham số) ném ra ngay vì thử lại cũng vô ích.
    """
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(
                model=config.MODEL,
                contents=contents,
                config=config_obj,
            )
        except (errors.ServerError, errors.ClientError) as e:
            # Chỉ retry với lỗi tạm thời: 503 (quá tải), 429 (quá giới hạn tốc độ)
            code = getattr(e, "code", None)
            if code in (503, 429) and attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s, 8s...
                print(f"[Retry] Model bận (lỗi {code}), chờ {wait}s rồi thử lại "
                      f"(lần {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue
            # Lỗi khác hoặc đã hết số lần thử -> ném ra
            raise
    # Không bao giờ tới đây, nhưng để an toàn
    raise RuntimeError("Đã hết số lần thử gọi model.")


def run_agent(system_prompt: str, user_input: str) -> str:
    """Vòng lặp Agent core dùng chung cho cả hai luồng Pre-merge và Post-deploy.

    Khởi tạo Gemini client, tắt automatic_function_calling để tự xử lý các tool call,
    gọi tool từ context/tools.py và phản hồi cho model đến khi đưa ra kết luận cuối cùng.

    Args:
        system_prompt (str): System prompt quy định vai trò (Pre-merge hoặc Post-deploy).
        user_input (str): Dữ liệu đầu vào (Code diff từ PR hoặc Runtime log đã parse).

    Returns:
        str: Kết luận đánh giá cuối cùng của Agent (PASS / WARN kèm giải thích).
    """
    client = genai.Client(api_key=config.GEMINI_API_KEY)

    # Cấu hình danh sách tools và tắt automatic function calling
    tools = [query_rules]
    config_obj = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
        temperature=0.2,
    )

    # Khởi tạo ngữ cảnh hội thoại
    contents = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_input)]
        )
    ]

    max_turns = 8
    turn = 0

    while turn < max_turns:
        turn += 1
        # Gọi model qua hàm có retry, thay vì gọi trực tiếp
        response = _call_model_with_retry(client, contents, config_obj)

        # Lưu trữ phản hồi của mô hình vào lịch sử ngữ cảnh
        if response.candidates and response.candidates[0].content:
            contents.append(response.candidates[0].content)

        # Kiểm tra xem mô hình có yêu cầu gọi function/tool hay không
        if response.function_calls:
            tool_parts = []
            for call in response.function_calls:
                func_name = call.name
                func_args = call.args or {}
                print(f"\n[Agent Tool Call] Function: {func_name} | Args: {func_args}")

                if func_name in AVAILABLE_TOOLS:
                    try:
                        tool_result = AVAILABLE_TOOLS[func_name](**func_args)
                    except Exception as err:
                        tool_result = f"Lỗi khi thực thi tool {func_name}: {str(err)}"
                else:
                    tool_result = f"Tool '{func_name}' không tồn tại trong hệ thống."

                print(f"[Agent Tool Result] Result: {tool_result}")
                tool_parts.append(
                    types.Part.from_function_response(
                        name=func_name,
                        response={"result": tool_result}
                    )
                )

            # Gửi kết quả tool về cho agent ở lượt tiếp theo
            contents.append(
                types.Content(
                    role="user",
                    parts=tool_parts
                )
            )
        else:
            # Mô hình đã hoàn tất suy luận và đưa ra kết luận
            return response.text or "Không nhận được nội dung phản hồi từ agent."

    return "Agent vượt quá số lượt gọi tối đa (max_turns) mà chưa hoàn tất kết luận."
