"""需要跳过的二进制文件扩展名（用于基于文本的操作）。

这些文件无法作为文本进行有意义的比较，并且通常体积较大。
移植自 free-code 的 src/constants/files.ts。
"""

BINARY_EXTENSIONS = frozenset({
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    # 视频
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    # 音频
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
    # 归档
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso",
    # 可执行文件/二进制
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib",
    ".app", ".msi", ".deb", ".rpm",
    # 文档（排除 .pdf——它是文本格式，agent 可能需要查看）
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    # 字体
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # 字节码 / 虚拟机产物
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear", ".node", ".wasm", ".rlib",
    # 数据库文件
    ".sqlite", ".sqlite3", ".db", ".mdb", ".idx",
    # 设计 / 3D
    ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd", ".blend", ".3ds", ".max",
    # Flash
    ".swf", ".fla",
    # 锁/性能分析数据
    ".lockb", ".dat", ".data",
})


def has_binary_extension(path: str) -> bool:
    """检查文件路径是否带有二进制扩展名。仅做字符串检查，不进行任何 I/O。"""
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:].lower() in BINARY_EXTENSIONS
