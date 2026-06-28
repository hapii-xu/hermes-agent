"""Hermes CLI 的共享 curses TUI 组件。

供 `hermes tools` 和 `hermes skills` 的交互式清单使用。
提供带键盘导航的 curses 多选组件，以及针对不支持 curses 的终端
的纯文本编号备选方案。
"""
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Set

from hermes_cli.colors import Colors, color


def _query_matches(label: str, query: str) -> bool:
    """当每个 query token 都是大小写不敏感的子序列时返回 True。"""
    normalized = label.lower()
    tokens = query.lower().split()

    if not tokens:
        return True

    for token in tokens:
        pos = 0

        for ch in token:
            pos = normalized.find(ch, pos)

            if pos < 0:
                return False

            pos += 1

    return True


_WORD_BOUNDARY = frozenset("-_/. ")


def _is_boundary(target: str, index: int) -> bool:
    """如果 ``target`` 中位置 ``index`` 处开始一个单词则返回 True。

    镜像 TS 评分器中的 ``isBoundary``：字符串开头、分隔符字符之后、
    或小写到大写的驼峰命名转换。
    """
    if index == 0:
        return True

    prev = target[index - 1]

    if prev in _WORD_BOUNDARY:
        return True

    # camelCase / 小写到大写转换（例如 `gptO` 中的 `O`）。
    cur = target[index]

    return prev == prev.lower() and cur != cur.lower() and cur == cur.upper()


def _token_score(orig: str, lower: str, token: str) -> float | None:
    """对一个 token 与目标进行评分。如果 token 不是子序列则返回 None。

    忠实移植自 ui-tui/src/lib/fuzzy.ts 和 web/src/lib/fuzzy.ts 中的
    ``fuzzyScore``，使三个界面以相同方式排列 model id：连续匹配、
    单词边界/首字符匹配、前缀匹配和精确匹配的得分都高于分散的子序列匹配。

    ``lower`` 是 ``orig`` 的小写形式；匹配针对 ``lower`` 进行，而
    边界检测使用 ``orig``（以便驼峰规则生效），与 TS 评分器完全一致。
    """
    score = 0.0
    prev = -1
    search_from = 0
    positions: list[int] = []

    for ch in token:
        idx = lower.find(ch, search_from)

        if idx < 0:
            return None

        positions.append(idx)
        score += 1

        if prev >= 0 and idx == prev + 1:
            score += 5
        elif prev >= 0:
            score -= min(idx - prev - 1, 3)

        if _is_boundary(orig, idx):
            score += 3

        if idx == 0:
            score += 5

        prev = idx
        search_from = idx + 1

    # 前缀加分：token 匹配了目标的连续前缀。
    if positions and positions[0] == 0 and positions[-1] == len(positions) - 1:
        score += 8

    # 精确完全匹配优先于其他所有情况。
    if lower == token:
        score += 20

    # 当得分接近时，稍微偏好更短的目标。
    score -= len(lower) * 0.01

    return score


def _fuzzy_score(label: str, query: str) -> float | None:
    """多 token 查询（AND）的聚合评分。如果任一 token 不匹配则返回 None。

    镜像 TS 评分器中的 ``fuzzyScoreMulti``：每个空白分隔的 token 都必须
    匹配；各 token 得分相加。
    """
    lower = label.lower()
    tokens = query.lower().split()

    if not tokens:
        return 0.0

    total = 0.0

    for token in tokens:
        token_score = _token_score(label, lower, token)

        if token_score is None:
            return None

        total += token_score

    return total


def _filter_indices(items: List[str], query: str) -> List[int]:
    """返回匹配 *query* 的项目索引，按最佳匹配优先排序。

    空查询保留所有项目且保持原始顺序。否则项目经过模糊匹配过滤并按
    得分降序排列，得分相同时按原始索引排序，使得分相同的行保持其
    目录顺序。
    """
    q = query.strip()

    if not q:
        return list(range(len(items)))

    scored = []

    for i, label in enumerate(items):
        score = _fuzzy_score(label, q)

        if score is not None:
            scored.append((i, score))

    scored.sort(key=lambda pair: (-pair[1], pair[0]))

    return [i for i, _ in scored]


@dataclass
class _SearchState:
    """curses 选择器循环共享的可变搜索状态。"""

    active: bool = False
    query: str = ""


def _reconcile_cursor(filtered: List[int], cursor: int) -> tuple[int, int]:
    """在过滤后的索引列表内返回 ``(cursor, cursor_pos)``。"""
    if not filtered:
        return cursor, 0

    if cursor not in filtered:
        cursor = filtered[0]

    return cursor, filtered.index(cursor)


def _move_filtered_cursor(
    filtered: List[int], cursor: int, cursor_pos: int, delta: int
) -> int:
    """在过滤后的索引列表中移动光标，像传统菜单一样循环。"""
    if not filtered:
        return cursor

    return filtered[(cursor_pos + delta) % len(filtered)]


def _scroll_for_cursor(
    scroll_offset: int, cursor_pos: int, visible_rows: int, total_rows: int
) -> int:
    """钳制滚动偏移量，使光标保持可见。"""
    visible_rows = max(1, visible_rows)

    if cursor_pos < scroll_offset:
        scroll_offset = cursor_pos
    elif cursor_pos >= scroll_offset + visible_rows:
        scroll_offset = cursor_pos - visible_rows + 1

    return max(0, min(scroll_offset, max(0, total_rows - visible_rows)))


def _handle_active_search_key(
    curses_mod, key: int, search: _SearchState
) -> tuple[bool, bool, bool]:
    """在搜索提示激活时处理按键。

    返回 ``(handled, confirm, changed)``。活跃搜索消耗查询编辑键，
    但将导航键留给菜单循环处理。
    """
    if not search.active:
        return False, False, False

    if key == 27:
        # Esc 停止搜索并清除查询，恢复完整列表（以便无匹配过滤
        # 不会让用户卡在空列表上）。当存在查询时发出 `changed` 信号，
        # 让驱动重置滚动/光标。
        had_query = bool(search.query)
        search.active = False
        search.query = ""
        return True, False, had_query

    if key in (curses_mod.KEY_BACKSPACE, 127, 8):
        search.query = search.query[:-1]
        return True, False, True

    if key == 21:  # Ctrl+U
        search.query = ""
        return True, False, True

    if key in (curses_mod.KEY_ENTER, 10, 13):
        return True, True, False

    if 32 <= key < 127:  # 可打印 ASCII；避免 128-255 的 Latin-1 乱码
        search.query += chr(key)
        return True, False, True

    return False, False, False


def flush_stdin() -> None:
    """刷新 stdin 输入缓冲区中的残留字节。

    必须在 ``curses.wrapper()``（或 simple_term_menu 等任何终端模式
    库）返回**之后**、下一次 ``input()`` / ``getpass.getpass()`` 调用
    **之前**调用。``curses.endwin()`` 恢复终端但不会清空 OS 输入
    缓冲区 — 残留的转义序列字节（来自方向键、终端模式切换响应
    或快速按键）仍然缓冲，会被下一次 ``input()`` 调用静默消费，
    破坏用户数据（例如将 ``^[^[`` 写入 .env 文件）。

    在非 TTY stdin（管道、重定向）或 Windows 上，这是 no-op。
    """
    try:
        if not sys.stdin.isatty():
            return
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


# ``read_menu_key`` 返回的标准化菜单动作。使用哨兵值使每个菜单的
# 按键处理分支保持一致，无需处理原始转义字节逻辑。
NAV_UP = "up"
NAV_DOWN = "down"
NAV_SELECT = "select"
NAV_TOGGLE = "toggle"
NAV_CANCEL = "cancel"
NAV_NONE = "none"


def read_menu_key(stdscr) -> str:
    """读取一次按键并将其标准化为菜单动作。

    除了 ``curses.KEY_*`` 翻译值外，还解码原始方向键转义序列。
    即使设置了 ``keypad(True)``（``curses.wrapper`` 会设置），某些
    终端/terminfo 条目仍以原始 CSI/SS3 字节序列传递光标键 —
    ``getch()`` 然后返回 ``27``（ESC），接着例如 ``[`` ``A``。
    将开头的 ``27`` 视为取消，正是导致设置向导的 provider/model
    选择器在用户按上/下时退出到编号备选方案的原因。

    返回 ``NAV_*`` 常量之一。单独的 ESC（在短时间内无后续字节）
    是通过转义路径映射到 ``NAV_CANCEL`` 的唯一情况；``q`` 也可以
    取消。未知序列映射到 ``NAV_NONE``，调用方可以忽略它们而不会
    误触发。
    """
    return _decode_menu_key(stdscr, stdscr.getch())


def _decode_menu_key(stdscr, key: int) -> str:
    """将已读取的按键标准化为菜单动作。

    从 ``read_menu_key`` 中拆分出来，以便感知搜索的循环可以在回退
    到导航解码之前检查原始键（例如捕获 ``/``）。
    """
    import curses

    if key in (curses.KEY_UP, ord("k")):
        return NAV_UP
    if key in (curses.KEY_DOWN, ord("j")):
        return NAV_DOWN
    if key in (curses.KEY_ENTER, 10, 13):
        return NAV_SELECT
    if key == ord(" "):
        return NAV_TOGGLE
    if key == ord("q"):
        return NAV_CANCEL

    if key == 27:  # ESC — 可能是单独的 ESC（取消）或转义序列。
        # 短暂等待后续字节。在慢速 PTY（SSH/tmux）上，方向键的字节
        # 可能分多次 read 到达，因此微小的超时可以避免将拆分的序列
        # 误读为单独的 ESC。
        try:
            stdscr.timeout(60)
            nxt = stdscr.getch()
        finally:
            stdscr.timeout(-1)  # 恢复阻塞模式

        if nxt == -1:
            return NAV_CANCEL  # 真正的单独 ESC

        if nxt in (ord("["), ord("O")):  # CSI / SS3 引入符
            final = stdscr.getch()
            if final in (ord("A"), ord("k")):
                return NAV_UP
            if final in (ord("B"), ord("j")):
                return NAV_DOWN
            # 消费任何其他 CSI 序列的尾部（例如 ``[3~`` Delete、
            # ``[H`` Home）直到其终止符，避免残留字节泄漏到下一次
            # input() 并破坏它。
            while 0x20 <= final <= 0x3F:  # CSI 参数/中间字节
                final = stdscr.getch()
            return NAV_NONE
        # ESC 后跟某个我们不处理的其他字节 — 吞掉它。
        return NAV_NONE

    return NAV_NONE


# 哨兵值：on_action reducer 返回此值表示"继续循环"（按键改变了
# 光标/选择状态但未完成菜单）。
_KEEP = object()


def _run_curses_menu(
    *,
    initial_cursor,
    item_count,
    draw_header,
    draw_row,
    on_action,
    reserve_bottom=1,
    draw_footer=None,
    extra_color_pairs=False,
    fallback,
    cancel_value,
    searchable=False,
    search_labels=None,
):
    """共享的 curses 单选/多选事件循环。

    拥有三个公共菜单过去逐字重复的所有内容：非 TTY 守卫、
    ``curses.wrapper`` 设置（隐藏光标 + 颜色对）、每帧的
    ``clear``/``getmaxyx``/``refresh`` 循环、滚动偏移计算、行迭代、
    ``read_menu_key`` 分发及 ``NAV_UP``/``NAV_DOWN`` 光标循环、
    ``flush_stdin``、以及 ``KeyboardInterrupt`` / curses 不可用时的
    回退。每个菜单的行为通过回调提供，使渲染输出与旧的手工循环
    保持字节级一致。

    回调/参数：
        draw_header(stdscr, max_y, max_x) -> int
            绘制标题/提示/描述行。返回可滚动项目列表应开始的
            第一个屏幕行索引。当搜索激活时，通过可选的 ``search``
            关键字接收实时的 ``_SearchState``（由菜单绘制，以便
            提示行可以显示它）。
        draw_row(stdscr, y, idx, is_cursor, max_x) -> None
            绘制一个项目行。``idx`` 始终是原始项目索引，因此无论
            过滤是否激活，每个菜单的渲染都保持不变。
        on_action(action, cursor) -> value
            SELECT/TOGGLE/CANCEL 的 reducer。返回 ``_KEEP`` 继续循环；
            返回其他任何值则以该值结束菜单。
            （UP/DOWN 光标移动由驱动本身处理。）
        reserve_bottom：保持不清项目的底部屏幕行数
            （1 = 最后一行留空，与旧循环一致）。
        draw_footer(stdscr, max_y, max_x) -> None
            可选的底部行绘制器（例如状态栏）。在项目行之后绘制；
            其行预算必须包含在 ``reserve_bottom`` 中。
        extra_color_pairs：同时初始化颜色对 3（暗灰色）用于状态栏。
        fallback() -> value
            当 curses 在真实 TTY 上出错（curses 不可用）时调用。
        cancel_value：在非 TTY stdin、ESC/取消或 KeyboardInterrupt 时返回。
        searchable：为 true 时，``/`` 打开基于 ``search_labels`` 的
            输入过滤提示。返回值始终是原始项目索引。
        search_labels：用于过滤的每个项目文本（``searchable`` 为 true
            时必需；长度必须等于 ``item_count``）。
    """
    # 非 TTY（管道/重定向 stdin）：curses 和 input() 都会挂起或空转，
    # 因此直接返回取消值 — 与重构前每个菜单中的守卫一致（编号备选
    # 方案仅用于真实 TTY 上的 curses 错误）。
    if not sys.stdin.isatty():
        return cancel_value

    use_search = searchable and search_labels is not None and len(search_labels) == item_count

    try:
        import curses
        result_holder = [_KEEP]

        def _draw(stdscr):
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                if extra_color_pairs:
                    curses.init_pair(
                        3, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1
                    )
            cursor = initial_cursor
            scroll_offset = 0
            search = _SearchState()
            # 用于过滤的非 None 标签；搜索禁用时为空，使
            # _filter_indices 保持廉价的恒等范围。
            labels: List[str] = (
                search_labels if (use_search and search_labels is not None) else []
            )

            while True:
                stdscr.clear()
                max_y, max_x = stdscr.getmaxyx()

                filtered = (
                    _filter_indices(labels, search.query)
                    if use_search
                    else list(range(item_count))
                )
                cursor, cursor_pos = _reconcile_cursor(filtered, cursor)

                # draw_header 在菜单想渲染实时过滤器时接受可选的
                # `search` 关键字；兼容不使用的 header。
                try:
                    items_start = draw_header(stdscr, max_y, max_x, search=search)
                except TypeError:
                    items_start = draw_header(stdscr, max_y, max_x)

                visible_rows = max(1, max_y - items_start - reserve_bottom)
                scroll_offset = _scroll_for_cursor(
                    scroll_offset, cursor_pos, visible_rows, len(filtered)
                )

                if use_search and search.query and not filtered:
                    try:
                        stdscr.addnstr(items_start, 0, "  No matches", max_x - 1, curses.A_DIM)
                    except curses.error:
                        pass

                for draw_i, filtered_pos in enumerate(
                    range(scroll_offset, min(len(filtered), scroll_offset + visible_rows))
                ):
                    i = filtered[filtered_pos]
                    y = draw_i + items_start
                    if y >= max_y - reserve_bottom:
                        break
                    draw_row(stdscr, y, i, i == cursor, max_x)

                if draw_footer is not None:
                    draw_footer(stdscr, max_y, max_x)

                stdscr.refresh()

                if use_search:
                    key = stdscr.getch()

                    if search.active:
                        # 活跃搜索消耗查询编辑键；导航键
                        # 继续到下方解码。
                        handled, confirm, changed = _handle_active_search_key(
                            curses, key, search
                        )
                        if changed:
                            scroll_offset = 0
                            cursor, cursor_pos = _reconcile_cursor(
                                _filter_indices(search_labels, search.query), cursor
                            )
                        if confirm:
                            if filtered:
                                outcome = on_action(NAV_SELECT, cursor)
                                if outcome is not _KEEP:
                                    result_holder[0] = outcome
                                    return
                            continue
                        if handled:
                            continue
                        action = _decode_menu_key(stdscr, key)
                    elif key == ord("/"):
                        search.active = True
                        continue
                    else:
                        action = _decode_menu_key(stdscr, key)
                else:
                    action = read_menu_key(stdscr)

                if action == NAV_UP:
                    cursor = _move_filtered_cursor(filtered, cursor, cursor_pos, -1)
                elif action == NAV_DOWN:
                    cursor = _move_filtered_cursor(filtered, cursor, cursor_pos, 1)
                elif action in (NAV_SELECT, NAV_TOGGLE, NAV_CANCEL):
                    if action == NAV_SELECT and use_search and not filtered:
                        continue
                    outcome = on_action(action, cursor)
                    if outcome is not _KEEP:
                        result_holder[0] = outcome
                        return

        curses.wrapper(_draw)
        flush_stdin()
        return result_holder[0] if result_holder[0] is not _KEEP else cancel_value

    except KeyboardInterrupt:
        return cancel_value
    except Exception:
        return fallback()


def curses_checklist(
    title: str,
    items: List[str],
    selected: Set[int],
    *,
    cancel_returns: Set[int] | None = None,
    status_fn: Optional[Callable[[Set[int]], str]] = None,
) -> Set[int]:
    """Curses 多选清单。返回选中索引的集合。

    参数：
        title：清单上方显示的标题行。
        items：每行的显示标签。
        selected：起始时已选中（预选）的索引。
        cancel_returns：ESC/q 时返回。默认为原始的 *selected*。
        status_fn：可选回调 ``f(chosen_indices) -> str``，其返回值
            在终端的最后一行渲染。用于实时汇总信息（例如预估 token 数）。
    """
    if cancel_returns is None:
        cancel_returns = set(selected)

    chosen = set(selected)

    def _draw_header(stdscr, max_y, max_x):
        import curses
        try:
            hattr = curses.A_BOLD
            if curses.has_colors():
                hattr |= curses.color_pair(2)
            stdscr.addnstr(0, 0, title, max_x - 1, hattr)
            stdscr.addnstr(
                1, 0,
                "  ↑↓ navigate  SPACE toggle  ENTER confirm  ESC cancel",
                max_x - 1, curses.A_DIM,
            )
        except curses.error:
            pass
        return 3

    def _draw_row(stdscr, y, i, is_cursor, max_x):
        import curses
        check = "✓" if i in chosen else " "
        arrow = "→" if is_cursor else " "
        line = f" {arrow} [{check}] {items[i]}"
        attr = curses.A_NORMAL
        if is_cursor:
            attr = curses.A_BOLD
            if curses.has_colors():
                attr |= curses.color_pair(1)
        try:
            stdscr.addnstr(y, 0, line, max_x - 1, attr)
        except curses.error:
            pass

    def _draw_footer(stdscr, max_y, max_x):
        import curses
        try:
            status_text = status_fn(chosen)
            if status_text:
                # 在最后一行右对齐
                sx = max(0, max_x - len(status_text) - 1)
                sattr = curses.A_DIM
                if curses.has_colors():
                    sattr |= curses.color_pair(3)
                stdscr.addnstr(max_y - 1, sx, status_text, max_x - sx - 1, sattr)
        except curses.error:
            pass

    def _on_action(action, cursor):
        if action == NAV_TOGGLE:
            chosen.symmetric_difference_update({cursor})
            return _KEEP
        if action == NAV_SELECT:
            return set(chosen)
        return cancel_returns  # NAV_CANCEL

    return _run_curses_menu(
        initial_cursor=0,
        item_count=len(items),
        draw_header=_draw_header,
        draw_row=_draw_row,
        on_action=_on_action,
        reserve_bottom=(2 if status_fn else 1),
        draw_footer=_draw_footer if status_fn else None,
        extra_color_pairs=bool(status_fn),
        fallback=lambda: _numbered_fallback(title, items, selected, cancel_returns, status_fn),
        cancel_value=cancel_returns,
    )


def curses_radiolist(
    title: str,
    items: List[str],
    selected: int = 0,
    *,
    cancel_returns: int | None = None,
    description: str | None = None,
    searchable: bool = False,
) -> int:
    """Curses 单选列表。返回选中的索引。

    参数：
        title：列表上方显示的标题行。
        items：每行的显示标签。
        selected：起始时选中的索引（预选）。
        cancel_returns：ESC/q 时返回。默认为原始的 *selected*。
        description：标题和项目列表之间显示的可选多行文本。
            用于在 curses 屏幕清除后仍需保留的上下文信息。
        searchable：为 true 时，``/`` 打开输入过滤提示。返回值
            始终是原始项目索引，而非过滤后的行位置。
    """
    if cancel_returns is None:
        cancel_returns = selected

    desc_lines: list[str] = []
    if description:
        desc_lines = description.splitlines()

    def _draw_header(stdscr, max_y, max_x, search=None):
        import curses
        row = 0
        try:
            hattr = curses.A_BOLD
            if curses.has_colors():
                hattr |= curses.color_pair(2)
            stdscr.addnstr(row, 0, title, max_x - 1, hattr)
            row += 1

            # 描述行
            for dline in desc_lines:
                if row >= max_y - 1:
                    break
                stdscr.addnstr(row, 0, dline, max_x - 1, curses.A_NORMAL)
                row += 1

            if searchable and search is not None and search.active:
                hint = f"  Search: {search.query}\u258e  BACKSPACE edit  Ctrl+U clear  ESC stop"
            elif searchable:
                hint = "  \u2191\u2193 navigate  ENTER/SPACE select  / search  ESC cancel"
            else:
                hint = "  \u2191\u2193 navigate  ENTER/SPACE select  ESC cancel"
            stdscr.addnstr(row, 0, hint, max_x - 1, curses.A_DIM)
            row += 1
        except curses.error:
            pass
        # 提示和项目列表之间的一个空行。
        return row + 1

    def _draw_row(stdscr, y, i, is_cursor, max_x):
        import curses
        radio = "\u25cf" if i == selected else "\u25cb"
        arrow = "\u2192" if is_cursor else " "
        line = f" {arrow} ({radio}) {items[i]}"
        attr = curses.A_NORMAL
        if is_cursor:
            attr = curses.A_BOLD
            if curses.has_colors():
                attr |= curses.color_pair(1)
        try:
            stdscr.addnstr(y, 0, line, max_x - 1, attr)
        except curses.error:
            pass

    def _on_action(action, cursor):
        if action in (NAV_SELECT, NAV_TOGGLE):
            return cursor
        return cancel_returns  # NAV_CANCEL

    return _run_curses_menu(
        initial_cursor=selected,
        item_count=len(items),
        draw_header=_draw_header,
        draw_row=_draw_row,
        on_action=_on_action,
        reserve_bottom=1,
        fallback=lambda: _radio_numbered_fallback(title, items, selected, cancel_returns),
        cancel_value=cancel_returns,
        searchable=searchable,
        search_labels=list(items) if searchable else None,
    )


def _radio_numbered_fallback(
    title: str,
    items: List[str],
    selected: int,
    cancel_returns: int,
) -> int:
    """单选的单选文本编号备选方案。"""
    print(color(f"\n  {title}", Colors.YELLOW))
    print(color("  Select by number, Enter to confirm.\n", Colors.DIM))

    for i, label in enumerate(items):
        marker = color("(\u25cf)", Colors.GREEN) if i == selected else "(\u25cb)"
        print(f"  {marker} {i + 1:>2}. {label}")
    print()
    try:
        val = input(color(f"  Choice [default {selected + 1}]: ", Colors.DIM)).strip()
        if not val:
            return selected
        idx = int(val) - 1
        if 0 <= idx < len(items):
            return idx
        return selected
    except (ValueError, KeyboardInterrupt, EOFError):
        return cancel_returns


def curses_single_select(
    title: str,
    items: List[str],
    default_index: int = 0,
    *,
    cancel_label: str = "Cancel",
    searchable: bool = False,
) -> int | None:
    """Curses 单选菜单。返回选中索引，取消时返回 None。

    在 prompt_toolkit 内也能工作，因为 curses.wrapper() 能安全恢复终端，
    不像 simple_term_menu 会与 /dev/tty 冲突。

    当 ``searchable`` 为 true 时，``/`` 打开输入过滤提示；返回值始终是
    原始项目索引（取消时为 None）。
    """
    all_items = list(items) + [cancel_label]
    cancel_idx = len(items)

    def _draw_header(stdscr, max_y, max_x, search=None):
        import curses
        try:
            hattr = curses.A_BOLD
            if curses.has_colors():
                hattr |= curses.color_pair(2)
            stdscr.addnstr(0, 0, title, max_x - 1, hattr)
            if searchable and search is not None and search.active:
                hint = f"  Search: {search.query}\u258e  BACKSPACE edit  Ctrl+U clear  ESC stop"
            elif searchable:
                hint = "  ↑↓ navigate  ENTER confirm  / search  ESC/q cancel"
            else:
                hint = "  ↑↓ navigate  ENTER confirm  ESC/q cancel"
            stdscr.addnstr(1, 0, hint, max_x - 1, curses.A_DIM)
        except curses.error:
            pass
        return 3

    def _draw_row(stdscr, y, i, is_cursor, max_x):
        import curses
        arrow = "→" if is_cursor else " "
        line = f" {arrow} {all_items[i]}"
        attr = curses.A_NORMAL
        if is_cursor:
            attr = curses.A_BOLD
            if curses.has_colors():
                attr |= curses.color_pair(1)
        try:
            stdscr.addnstr(y, 0, line, max_x - 1, attr)
        except curses.error:
            pass

    def _on_action(action, cursor):
        if action == NAV_SELECT:
            # 选中合成的取消行时解析为 None，镜像旧的
            # 循环后 ``>= cancel_idx`` 守卫。
            return None if cursor >= cancel_idx else cursor
        if action == NAV_CANCEL:
            return None
        return _KEEP  # NAV_TOGGLE — 此菜单的 no-op

    return _run_curses_menu(
        initial_cursor=min(default_index, len(all_items) - 1),
        item_count=len(all_items),
        draw_header=_draw_header,
        draw_row=_draw_row,
        on_action=_on_action,
        reserve_bottom=1,
        fallback=lambda: _numbered_single_fallback(title, all_items, cancel_idx),
        cancel_value=None,
        searchable=searchable,
        search_labels=list(all_items) if searchable else None,
    )


def _numbered_single_fallback(
    title: str,
    items: List[str],
    cancel_idx: int,
) -> int | None:
    """单选的文本编号备选方案。"""
    print(f"\n  {title}\n")
    for i, label in enumerate(items, 1):
        print(f"  {i}. {label}")
    print()
    try:
        val = input(f"  Choice [1-{len(items)}]: ").strip()
        if not val:
            return None
        idx = int(val) - 1
        if 0 <= idx < len(items) and idx < cancel_idx:
            return idx
        if idx == cancel_idx:
            return None
    except (ValueError, KeyboardInterrupt, EOFError):
        pass
    return None


def _numbered_fallback(
    title: str,
    items: List[str],
    selected: Set[int],
    cancel_returns: Set[int],
    status_fn: Optional[Callable[[Set[int]], str]] = None,
) -> Set[int]:
    """不支持 curses 的终端的文本切换备选方案。"""
    chosen = set(selected)
    print(color(f"\n  {title}", Colors.YELLOW))
    print(color("  Toggle by number, Enter to confirm.\n", Colors.DIM))

    while True:
        for i, label in enumerate(items):
            marker = color("[✓]", Colors.GREEN) if i in chosen else "[ ]"
            print(f"  {marker} {i + 1:>2}. {label}")
        if status_fn:
            status_text = status_fn(chosen)
            if status_text:
                print(color(f"\n  {status_text}", Colors.DIM))
        print()
        try:
            val = input(color("  Toggle # (or Enter to confirm): ", Colors.DIM)).strip()
            if not val:
                break
            idx = int(val) - 1
            if 0 <= idx < len(items):
                chosen.symmetric_difference_update({idx})
        except (ValueError, KeyboardInterrupt, EOFError):
            return cancel_returns
        print()

    return chosen
