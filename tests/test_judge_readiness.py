"""判定器の確認の結果を、しばらく使い回すことを検証する。"""

from poop_detector.video.judge_readiness import JudgeReadiness


def test_result_is_reused_until_it_gets_old():
    now = [0.0]
    asked = []

    def check(provider, model):
        asked.append((provider, model))
        return f"{provider} に{len(asked)}回目"

    readiness = JudgeReadiness(seconds=60, clock=lambda: now[0], check=check)
    assert readiness.problem("codex", "") == "codex に1回目"
    now[0] = 59
    assert readiness.problem("codex", "") == "codex に1回目"
    # 判定器やモデルが違えば、別に確かめる。
    assert readiness.problem("claude", "model-b") == "claude に2回目"
    now[0] = 60
    assert readiness.problem("codex", "") == "codex に3回目"
    assert asked == [("codex", ""), ("claude", "model-b"), ("codex", "")]
