def extract_world_id(location: str | None) -> str:
    if not location:
        return ""
    text = str(location).strip()
    if ':' in text:
        world_part = text.split(':', 1)[0]
        return world_part if world_part.startswith('wrld_') else ""
    return text if text.startswith('wrld_') else ""


def format_location(location: str | None) -> str:
    if not location:
        return "未知位置"

    text = str(location).strip()
    lowered = text.lower()

    if lowered == 'offline':
        return '离线'
    if lowered == 'private':
        return '私密'
    if lowered == 'traveling':
        return '旅行中'
    if lowered == 'unknown':
        return '未知位置'

    if ':' not in text:
        if text.startswith('wrld_'):
            return '某个世界'
        return text

    _, instance_part = text.split(':', 1)

    if '~hidden' in instance_part:
        return '隐藏实例'
    if '~friends' in instance_part:
        return '好友实例'
    if '~private' in instance_part:
        return '私有实例'
    if '~group' in instance_part:
        return '群组实例'
    if '~public' in instance_part:
        return '公开实例'

    return '世界实例'
