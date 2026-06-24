from datetime import datetime, timezone, timedelta
import re


def iso_to_cst(iso_time_str: str) -> str:
    """将 ISO 格式的时间字符串转换为 CST(China Standard Time) 时间并格式化为 %Y-%m-%d %H:%M:%S 格式

    Args:
        iso_time_str (str): ISO 格式时间字符串

    Returns:
        str: CST(China Standard Time) 时间字符串
    """
    dt = datetime.fromisoformat(iso_time_str)
    tz = timezone(timedelta(hours=8))
    dt_cst = dt if dt.astimezone(tz) > datetime.now(tz) else dt.astimezone(tz)
    return dt_cst.strftime("%Y-%m-%d %H:%M:%S") if dt_cst.year >= 1970 else ""


def _parse_single_size_text(text):
    normalized = str(text).replace("：", ":").replace("，", ",")
    match = re.search(r"大小\s*:\s*(-|[^\s,]+)", normalized, re.IGNORECASE)
    if match and match.group(1).strip() != "-":
        text_value = match.group(1).strip().upper()
    else:
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|G|M|K|T)\b",
            normalized,
            re.IGNORECASE,
        )
        if not match:
            return 0
        text_value = f"{match.group(1)}{match.group(2).upper()}"

    unit_map = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
    }
    m = re.match(r"^([\d.]+)\s*([A-Z]+)?$", text_value)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2) or "B"
    multiplier = unit_map.get(unit)
    if not multiplier:
        return 0
    return int(num * multiplier)


def parse_size_from_content(content):
    """从搜索结果描述中解析大小，如「大小:1.5GB」。"""
    return _parse_single_size_text(content) if content else 0


def parse_size_from_texts(*texts):
    """从多个文本字段中解析文件大小。"""
    for text in texts:
        size = parse_size_from_content(text)
        if size > 0:
            return size
    return 0
