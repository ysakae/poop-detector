"""解像度に依存しない監視領域。"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ROI:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        if not (
            0 <= self.x < 1
            and 0 <= self.y < 1
            and self.width > 0
            and self.height > 0
            and self.x + self.width <= 1
            and self.y + self.height <= 1
        ):
            raise ValueError("ROIは画像内の空でない領域を指定してください")

    @classmethod
    def from_pixels(cls, rect, width, height):
        x, y, w, h = rect
        return cls(x / width, y / height, w / width, h / height)

    def pixels(self, width, height):
        return (
            round(self.x * width),
            round(self.y * height),
            max(1, round(self.width * width)),
            max(1, round(self.height * height)),
        )

    def crop(self, frame):
        x, y, w, h = self.pixels(frame.shape[1], frame.shape[0])
        return frame[y : y + h, x : x + w]

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text()))
