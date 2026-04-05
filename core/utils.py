def extract_world_id(location: str | None) -> str:
    if not location:
        return ""
    text = str(location).strip()
    if ':' in text:
        world_part = text.split(':', 1)[0]
        return world_part if world_part.startswith('wrld_') else ""
    if '/' in text:
        world_part = text.split('/', 1)[0]
        return world_part if world_part.startswith('wrld_') else ""
    return text if text.startswith('wrld_') else ""


def _split_world_and_instance(location: str) -> tuple[str, str]:
    text = str(location or '').strip()
    if not text:
        return '', ''

    if ':' in text:
        world_part, instance_part = text.split(':', 1)
        world_part = world_part.strip()
        if world_part.startswith('wrld_'):
            return world_part, instance_part.strip()

    if '/' in text:
        world_part, instance_part = text.split('/', 1)
        world_part = world_part.strip()
        if world_part.startswith('wrld_'):
            return world_part, instance_part.strip()

    world_id = extract_world_id(text)
    if world_id:
        return world_id, ''
    return '', ''


def get_location_group_key(location: str | None) -> str:
    if not location:
        return ""
    text = str(location).strip()
    lowered = text.lower()
    if lowered in {'offline', 'unknown', 'traveling', 'travelling', 'private'}:
        return ""

    world_id, instance_raw = _split_world_and_instance(text)
    if not world_id:
        return ""
    if not instance_raw:
        return world_id

    instance_id = instance_raw.split('~', 1)[0].strip()
    if not instance_id:
        return world_id

    mode = _parse_instance_access_mode(text)
    mode_suffix = {
        'public': '~public',
        'hidden': '~hidden',
        'friends': '~friends',
        'private': '~private',
        'group': '~group',
    }.get(mode, '')
    return f"{world_id}:{instance_id}{mode_suffix}"


def _parse_instance_access_mode(location: str | None) -> str:
    """Return normalized access mode for instance part.

    possible values: public / hidden / friends / private / group / unknown
    """
    if not location:
        return 'unknown'
    text = str(location).strip().lower()
    if ':' not in text:
        if '/' in text:
            instance_part = text.split('/', 1)[1]
        else:
            return 'unknown'
    else:
        instance_part = text.split(':', 1)[1]

    if '~public' in instance_part:
        return 'public'
    if '~hidden' in instance_part:
        return 'hidden'  # friends+
    if '~friends' in instance_part:
        return 'friends'
    if '~private' in instance_part:
        return 'private'  # invite / invite+
    if '~group' in instance_part:
        return 'group'
    return 'unknown'


def infer_joinability(location: str | None, status: str | None = None) -> str:
    status_text = (status or '').strip().lower()
    if status_text == 'offline':
        return '不可进入'

    if not location:
        return '未知'

    lowered = str(location).strip().lower()
    if lowered in {'offline', 'private'}:
        return '不可进入'
    if lowered in {'unknown', 'traveling', 'travelling'}:
        return '未知'

    mode = _parse_instance_access_mode(location)
    if mode in {'public', 'hidden', 'friends'}:
        return '可加入'
    if mode == 'private':
        return '不可进入'
    if mode == 'group':
        return '未知'
    return '未知'


def format_location(location: str | None) -> str:
    if not location:
        return '未知位置'

    text = str(location).strip()
    lowered = text.lower()

    if lowered == 'offline':
        return '离线'
    if lowered == 'private':
        return '仅限邀请'
    if lowered in {'traveling', 'travelling'}:
        return '旅行中'
    if lowered == 'unknown':
        return '未知位置'

    if ':' not in text and '/' not in text:
        if text.startswith('wrld_'):
            return '某个世界'
        return text

    mode = _parse_instance_access_mode(text)
    if mode == 'hidden':
        return '好友+实例'
    if mode == 'friends':
        return '仅限好友实例'
    if mode == 'private':
        return '仅限邀请实例'
    if mode == 'group':
        return '群组实例'
    if mode == 'public':
        return '公开实例'

    return '世界实例'
