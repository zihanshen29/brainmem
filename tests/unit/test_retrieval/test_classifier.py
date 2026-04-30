import pytest

from brain.pipeline.retrieval import classify_query


@pytest.mark.parametrize(
    "query",
    [
        "我在 2025 年做了什么?",
        "谁负责 computer vision 项目?",
        "小张什么时候帮过我?",
        "列出最近提到的项目",
        "有哪些人参与了论文阅读?",
    ],
)
def test_classify_structured_queries(query: str) -> None:
    assert classify_query(query) == "structured"


@pytest.mark.parametrize(
    "query",
    [
        "帮我回顾一下 computer vision 项目的背景",
        "Transformer 和 RNN 的关系是什么",
        "我最近的研究方向怎么样",
        "总结一下这段经历的意义",
        "为什么这个方案可能有效",
    ],
)
def test_classify_open_ended_queries(query: str) -> None:
    assert classify_query(query) == "open_ended"
