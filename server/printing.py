"""Order tickets for Star CloudPRNT printers (mC-Print3, TSP143IV).

Each job is two tickets separated by a cut: a kitchen copy (big number,
name, slot, items) and a bag copy (items, total, "paid online", QR to the
customer's order page). Built as raw StarPRNT commands for 80mm paper,
with a plain-text fallback for printers that only take text/plain.
"""

COLS = 48  # Font A on 80mm paper

ESC, GS, LF = b"\x1b", b"\x1d", b"\n"
INIT = ESC + b"@" + ESC + GS + b"t\x01"  # reset, code page 437 (has £)
BOLD_ON, BOLD_OFF = ESC + b"E", ESC + b"F"
CUT = ESC + b"d\x03"  # feed and partial cut


def align(n):  # 0 left, 1 centre, 2 right
    return ESC + GS + b"a" + bytes([n])


def size(h, w):  # multipliers 1..6
    return ESC + b"i" + bytes([h - 1, w - 1])


def enc(text):
    return text.encode("cp437", "replace")


def qr(data, cell=6):
    d = data.encode("ascii", "replace")
    return (ESC + GS + b"yS0\x02"            # model 2
            + ESC + GS + b"yS1\x01"          # error correction M
            + ESC + GS + b"yS2" + bytes([cell])
            + ESC + GS + b"yD1\x00" + len(d).to_bytes(2, "little") + d
            + ESC + GS + b"yP")


def gbp(pence):
    return f"£{pence / 100:.2f}"


def short(name):
    return name.replace("The ", "").replace(" Caesar", "")


def two_col(left, right, width=COLS):
    left = left[: width - len(right) - 1]
    return left + " " * (width - len(left) - len(right)) + right


def ticket_lines(order, order_url):
    """Both copies as (style, text) lines — shared by the StarPRNT and text builders.

    style: "code" huge, "big" double, "bold", "centre", "rule", "qr", "cut", or "" plain.
    """
    items = order["items"]
    slot = f"{order['slot']}-{order['slotEnd']}"
    name = order["name"].upper()[:22]
    lines = [
        ("centre", "ONLINE ORDER - PAID"),
        ("code", order["code"]),
        ("big", name),
        ("bold", f"COLLECT {slot}"),
        ("rule", ""),
    ]
    lines += [("big-left", f"{i['qty']} x {short(i['name'])}") for i in items]
    lines += [("rule", ""), ("centre", f"{sum(i['qty'] for i in items)} wraps  |  {order['slotDay']}"), ("cut", "")]

    lines += [
        ("big", "JULIUS."),
        ("centre", "Caesar chicken wraps"),
        ("centre", ""),
        ("code", order["code"]),
        ("bold", order["name"][:COLS]),
        ("centre", f"Collect {order['slotDay'].lower()}, {slot}"),
        ("rule", ""),
    ]
    lines += [("", two_col(f"{i['qty']} x {i['name']}", gbp(i["qty"] * i["price"]))) for i in items]
    lines += [
        ("rule", ""),
        ("bold-left", two_col("TOTAL", gbp(order["total"]))),
        ("", "Paid online - thank you"),
        ("centre", ""),
        ("qr", order_url),
        ("centre", "Scan to see your order"),
        ("centre", "eatjulius.com"),
        ("cut", ""),
    ]
    return lines


def starprnt(order, order_url):
    out = INIT
    for style, text in ticket_lines(order, order_url):
        if style == "cut":
            out += LF + LF + CUT
        elif style == "rule":
            out += align(0) + enc("-" * COLS) + LF
        elif style == "qr":
            out += align(1) + qr(text) + LF
        elif style == "code":
            out += align(1) + BOLD_ON + size(4, 4) + enc(text) + size(1, 1) + BOLD_OFF + LF
        elif style == "big":
            out += align(1) + BOLD_ON + size(2, 2) + enc(text) + size(1, 1) + BOLD_OFF + LF
        elif style == "big-left":
            out += align(0) + BOLD_ON + size(2, 2) + enc(text) + size(1, 1) + BOLD_OFF + LF
        elif style == "bold":
            out += align(1) + BOLD_ON + enc(text) + BOLD_OFF + LF
        elif style == "bold-left":
            out += align(0) + BOLD_ON + enc(text) + BOLD_OFF + LF
        elif style == "centre":
            out += align(1) + enc(text) + LF
        else:
            out += align(0) + enc(text) + LF
    return out


def plain_text(order, order_url):
    rows = []
    for style, text in ticket_lines(order, order_url):
        if style == "cut":
            rows += ["", "", "- - - - - - - - - - cut - - - - - - - - - -", ""]
        elif style == "rule":
            rows.append("-" * COLS)
        elif style == "qr":
            rows.append(text.center(COLS))
        elif style in ("code", "big", "bold", "centre"):
            rows.append(text.center(COLS))
        else:
            rows.append(text)
    return "\n".join(rows) + "\n"
