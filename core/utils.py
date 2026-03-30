def extract_world_id(location: str | None) -> str:
    if not location:
        return ""
    text = str(location).strip()
    if ':' in text:
        world_part = text.split(':', 1)[0]
        return world_part if world_part.startswith('wrld_') else ""
    return text if text.startswith('wrld_') else ""


def get_location_group_key(location: str | None) -> str:
    if not location:
        return ""
    text = str(location).strip()
    lowered = text.lower()
    if lowered in {'offline', 'unknown', 'traveling', 'private'}:
        return ""
    if ':' in text:
        world_part = text.split(':', 1)[0]
        if world_part.startswith('wrld_'):
            return text
    world_id = extract_world_id(text)
    return world_id if world_id else ""


def infer_joinability(location: str | None, status: str | None = None) -> str:
    status_text = (status or '').strip().lower()
    if status_text == 'offline':
        return '不可加入'

    if not location:
        return '未知'

    text = str(location).strip()
    lowered = text.lower()

    if lowered == 'offline':
        return '不可加入'
    if lowered in {'unknown', 'traveling'}:
        return '未知'
    if lowered == 'private':
        return '不可加入'

    if ':' not in text:
        return '未知'

    instance_part = text.split(':', 1)[1].lower()
    if '~public' in instance_part:
        return '可加入'
    if '~friends' in instance_part:
        return '可加入'
    if '~private' in instance_part:
        return '不可加入'
    if '~hidden' in instance_part:
        return '不可加入'
    if '~group' in instance_part:
        return '需邀请'

    return '未知'


def format_location(location: str | None) -> str:
    if not location:
        return '未知位置'

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
