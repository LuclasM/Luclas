"""
memory/token_estimate.py — 不依赖第三方库的粗略 token 估算

没有接入 tiktoken 之类的精确分词器（本项目本来就避免上重依赖，见
llm_client.py 的 usage 只是调用后读取真实返回值，从不提前预估）。这里只是
给对话层判断"是否接近 context 上限"用的启发式估算，留足安全余量
（70% 才触发压缩），不需要精确。

中日韩字符大致 1 个字对应 1 个 token，其余字符大致 4 个字符对应 1 个
token——按这个混合比例估算。
"""

import re

_CJK_PATTERN = re.compile(
    r'[一-鿿㐀-䶿぀-ヿ가-힯]'
)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_chars = len(_CJK_PATTERN.findall(text))
    other_chars = len(text) - cjk_chars
    return cjk_chars + (other_chars + 3) // 4
