"""
图像识别降级方案 —— 当 DOM 不可读或内网页面渲染异常时启用
基于 Playwright screenshot + OpenCV 模板匹配
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Optional, Tuple

# 延迟导入（只有需要图像识别时才加载）
_cv2 = None
_np = None
_pytesseract = None


def _ensure_deps():
    global _cv2, _np, _pytesseract
    if _cv2 is None:
        try:
            import cv2; import numpy as np
            _cv2, _np = cv2, np
        except ImportError:
            raise ImportError("图像识别降级方案需要 opencv-python-headless 和 numpy，请先 pip install opencv-python-headless numpy")
    return _cv2, _np


async def find_on_screen(page, template_path: str, threshold: float = 0.85) -> Optional[Tuple[float, float, float]]:
    """
    截全屏 → OpenCV 模板匹配 → 返回 (x, y, confidence)
    找不到返回 None
    """
    cv2, np = _ensure_deps()

    # Playwright 截图保存到临时文件
    screenshot_bytes = await page.screenshot()
    img_arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
    screen = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)

    # 加载模板
    tpl_path = Path(template_path)
    if not tpl_path.exists():
        print(f"  [vision] 模板图片不存在: {template_path}")
        return None
    template = cv2.imread(str(tpl_path))
    if template is None:
        return None

    # 匹配
    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

    if max_val >= threshold:
        h, w = template.shape[:2]
        cx = max_loc[0] + w / 2
        cy = max_loc[1] + h / 2
        return cx, cy, float(max_val)
    return None


async def click_by_template(page, template_path: str, threshold: float = 0.85, label: str = "") -> bool:
    """通过模板匹配找到并点击"""
    found = await find_on_screen(page, template_path, threshold)
    if found is None:
        print(f"  [vision] 未找到匹配: {label or template_path} (阈值={threshold})")
        return False
    x, y, conf = found
    await page.mouse.click(x, y)
    print(f"  [vision] 点击成功: {label or template_path}  置信度={conf:.2f}  坐标=({int(x)},{int(y)})")
    return True


async def ocr_on_region(page, region: Tuple[int, int, int, int]) -> str:
    """
    对屏幕某个矩形区域截图后 OCR 识别
    region = (x, y, width, height) — 屏幕坐标
    """
    try:
        import pytesseract
    except ImportError:
        print("  [vision] OCR 需要 pytesseract + Tesseract-OCR 引擎")
        return ""

    cv2, np = _ensure_deps()
    screenshot_bytes = await page.screenshot(clip={"x": region[0], "y": region[1], "width": region[2], "height": region[3]})
    img_arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
    roi = cv2.imdecode(img_arr, cv2.IMREAD_GRAYSCALE)
    # 二值化增强
    _, thresh = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(thresh, lang="chi_sim+eng")
    return text.strip()
