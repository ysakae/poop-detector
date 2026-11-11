"""利用者に原因と対処を伝える失敗の種類。

ここの例外の文言は、このパッケージで書いたものとしてそのまま表示する。
外部の例外の文字列（RTSP の URL を含みうる）は入れない。表示の前には念のため
`redaction.redact` も通す。
"""


class UserError(Exception):
    """利用者に理由を伝えて終了コード 2 で終える失敗の基底。"""


class SetupRequiredError(UserError, ValueError):
    """監視範囲や基準画像などの登録がまだ済んでいない。"""


class ReferenceMismatchError(UserError, ValueError):
    """登録済みの基準画像が、いまの監視範囲や映像と合わない。"""


class CameraUnavailableError(UserError):
    """カメラから映像を取得できない。"""


class InvalidInputError(UserError, ValueError):
    """コマンドの引数や入力値が正しくない。"""


class AlreadyRunningError(UserError, ValueError):
    """同じデータを使う別の処理が動いている。"""


class IncompatibleDataError(UserError, ValueError):
    """保存済みのデータが、このバージョンでは扱えない形になっている。"""
