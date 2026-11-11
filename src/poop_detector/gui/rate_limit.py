"""接続元ごとの要求の回数の制限（総当たりや連打への対策）。

決めた時間の窓の中で数え、上限を超えた要求には 429 と待つ秒数を返す。数えた記録は
メモリーにだけ持ち、接続元の数にも上限を設ける。
"""

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

# (回数, 秒)。この秒数の間に、この回数まで受け付ける。
Rule: TypeAlias = tuple[int, float]
MAX_CLIENTS = 10000


@dataclass(frozen=True)
class RateLimits:
    """要求の種類ごとの上限。

    token は起動トークン付きの URL（正しいかどうかに関わらず数える）、post はフォームの送信、
    all は画像やライブ映像を含むすべての要求。
    """

    token: Rule = (10, 300.0)
    post: Rule = (60, 60.0)
    all: Rule = (1200, 60.0)


class RateLimiter:
    """(接続元, 種類) ごとに、窓の中の要求の時刻を覚える。clock はテストで差し替える。

    ASGI のイベントループの中だけで使うので、排他はいらない。
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = MAX_CLIENTS
    ) -> None:
        self.clock = clock
        self.max_keys = max_keys
        self._hits: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._hits)

    def hit(self, client: str, kind: str, rule: Rule) -> int:
        """要求を1回数える。受け付けるなら 0、上限を超えたら待つ秒数（1以上）。"""
        limit, window = rule
        now = self.clock()
        key = (client, kind)
        hits = self._hits.pop(key, None) or deque()
        while hits and hits[0] <= now - window:
            hits.popleft()
        self._hits[key] = hits  # 最近使った接続元として末尾に置く
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)
        if len(hits) >= limit:
            return max(1, math.ceil(hits[0] + window - now))
        hits.append(now)
        return 0
