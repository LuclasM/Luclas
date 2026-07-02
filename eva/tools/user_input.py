"""
tools/user_input.py — 任务执行中向用户提问，等待回答后继续
"""

import i18n as T
from utils.display import bold, warn

ASK_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Pause the current task to ask the user a question, then continue after they answer. "
            "Use this when you need information only the user can provide — a decision, a missing "
            "credential, a preference, or a clarification — and proceeding without it would produce "
            "wrong results. Do not use it to report progress or confirm steps you can verify yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to ask the user. Be concise and concrete.",
                },
            },
            "required": ["question"],
        },
    },
}


def ask_user(question: str) -> str:
    import sys
    if not sys.stdin.isatty():
        # 非终端（API/WeCom）：把问题抛回给调用方
        raise _NeedUserInput(question)

    print(f"\n{warn('─' * 50)}")
    print(f"  {bold(T.ask_user_label())}")
    print(f"\n  {question}\n")
    try:
        answer = input(f"  {T.ask_user_prompt()} ").strip()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt
    print(f"{warn('─' * 50)}\n")
    return answer if answer else T.ask_user_no_answer()


class _NeedUserInput(Exception):
    def __init__(self, question: str):
        self.question = question
        super().__init__(question)
