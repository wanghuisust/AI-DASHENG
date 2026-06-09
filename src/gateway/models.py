"""统一消息模型"""


class PlatformMessage:
    """跨平台统一消息"""
    __slots__ = ("platform", "user_id", "chat_id", "text", "is_group", "at_me", "raw")

    def __init__(self, platform: str, user_id: str, chat_id: str,
                 text: str, is_group: bool = False, at_me: bool = False, raw: dict = None):
        self.platform = platform   # "qq" | "wechat"
        self.user_id = user_id
        self.chat_id = chat_id
        self.text = text
        self.is_group = is_group
        self.at_me = at_me
        self.raw = raw or {}

    def __repr__(self):
        tag = "群" if self.is_group else "私"
        return f"<{self.platform}[{tag}] {self.user_id}@{self.chat_id}: {self.text[:30]}>"


class PlatformReply:
    """跨平台统一回复"""
    __slots__ = ("chat_id", "text", "platform", "is_group", "at_user", "raw")

    def __init__(self, chat_id: str, text: str, platform: str,
                 is_group: bool = False, at_user: str = "", raw: dict = None):
        self.chat_id = chat_id
        self.text = text
        self.platform = platform
        self.is_group = is_group
        self.at_user = at_user
        self.raw = raw or {}
