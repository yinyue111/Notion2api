import sys

from app.config import get_default_account
from app.conversation import ConversationManager
from app.notion_client import NotionOpusAPI


def main():
    try:
        account = get_default_account()
    except ValueError as e:
        print(f"[配置错误] {e}")
        sys.exit(1)

    client = NotionOpusAPI(account)
    manager = ConversationManager()

    print("=" * 40)
    print("        Notion Opus 终端       ")
    print(" 输入 'exit' 退出程序，输入 'new' 开始新对话。")
    print("=" * 40)

    current_conv = manager.new_conversation()

    while True:
        try:
            user_input = input("\n[You]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n退出程序...")
            break

        if not user_input:
            continue

        if user_input.lower() == "exit":
            print("退出程序...")
            break

        if user_input.lower() == "new":
            current_conv = manager.new_conversation()
            print("\n--- 已开启新对话 ---")
            continue

        transcript = manager.get_transcript(client, current_conv, user_input, "claude-opus4.6")

        print("\n[AI]: ", end="", flush=True)

        full_text = ""
        stream = client.stream_response(transcript)

        try:
            for item in stream:
                if isinstance(item, dict):
                    item_type = item.get("type")
                    if item_type == "content":
                        text = str(item.get("text", "") or "")
                        if text:
                            print(text, end="", flush=True)
                            full_text += text
                    elif item_type == "search":
                        search_data = item.get("data", {})
                        if isinstance(search_data, dict) and search_data.get("queries"):
                            print(f"\n[Search] {', '.join(search_data.get('queries', []))}\n", end="", flush=True)
                    continue

                if isinstance(item, str) and item:
                    print(item, end="", flush=True)
                    full_text += item
        except KeyboardInterrupt:
            print("\n[提示] 用户中断当前输出")
        except Exception as e:
            print(f"\n[错误]: 输出流解析异常 - {e}")

        print()

        if full_text:
            manager.add_message(current_conv, "user", user_input)
            manager.add_message(current_conv, "assistant", full_text)


if __name__ == "__main__":
    main()
