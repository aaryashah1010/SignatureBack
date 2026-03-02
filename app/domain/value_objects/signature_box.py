from dataclasses import dataclass


@dataclass(frozen=True)
class SignatureBox:
    page_number: int
    x: float
    y: float
    width: float
    height: float

    def validate(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number must be >= 1")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be greater than 0")
        if not (0 <= self.x <= 1 and 0 <= self.y <= 1):
            raise ValueError("x and y must be in [0, 1]")
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("signature box exceeds page bounds")

    def is_approximately_equal(self, other: "SignatureBox", tolerance: float = 0.005) -> bool:
        return (
            self.page_number == other.page_number
            and abs(self.x - other.x) <= tolerance
            and abs(self.y - other.y) <= tolerance
            and abs(self.width - other.width) <= tolerance
            and abs(self.height - other.height) <= tolerance
        )
