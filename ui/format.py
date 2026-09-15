"""GUI 顯示用的小型格式化 helper（跟 theme 搭配，不含業務邏輯）。"""

from ui.theme import COLOR_GAIN, COLOR_LOSS


def gain_loss_color(value):
    """依正負回傳台股慣例的漲跌色（漲＝紅、跌＝綠）；value 為 None 或 0 視為漲
    （沿用既有 `COLOR_GAIN if x >= 0 else COLOR_LOSS` 三元式的既有行為，不改語意）。"""
    return COLOR_GAIN if (value is None or value >= 0) else COLOR_LOSS
