"""领域输入错误：消息面向网页用户（中文）。"""


class InputError(Exception):
    """上传数据不满足约定时抛出，携带可直接展示的错误信息。"""

    def __init__(self, messages, title="输入数据有误"):
        if isinstance(messages, str):
            messages = [messages]
        self.messages = list(messages)
        self.title = title
        super().__init__("；".join(self.messages))
