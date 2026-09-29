"""Order confirmation email over plain SMTP.

Works with any SMTP provider (Resend, Postmark, Google Workspace, GoDaddy
mail...). Sends nothing and logs a line when SMTP_HOST isn't configured.
"""
import html
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "orders@eatjulius.com")
MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "hello@eatjulius.com")

GREEN, CREAM, INK, MUTED = "#1F3A2C", "#F3F0E8", "#1C1C1A", "#5E625B"


def gbp(pence):
    return f"£{pence / 100:.2f}"


def build(order, order_url):
    """(subject, text, html) for a paid order as returned by order_public()."""
    slot = f"{order['slot']}–{order['slotEnd']}"
    when = f"{order['slotDay'].lower()}, {slot}"
    subject = f"JULIUS order {order['code']} — collect {when}"

    lines = [f"{i['qty']} x {i['name']}  {gbp(i['qty'] * i['price'])}" for i in order["items"]]
    text = "\n".join([
        f"Thanks {order['name']}, your order is paid.",
        "",
        f"Order number: {order['code']}",
        f"Collect: {when}",
        "Where: JULIUS kiosk, Kiosk 4, Reuters Plaza, Canary Wharf",
        "",
        *lines,
        f"Total paid: {gbp(order['total'])}",
        "",
        "Skip the queue and go straight to the hand-off point. Show your order number.",
        f"Track your order: {order_url}",
        "",
        "JULIUS — Caesar, done right.",
    ])

    e = html.escape
    rows = "".join(
        f'<tr><td style="padding:6px 0;color:{INK}">{i["qty"]} × {e(i["name"])}</td>'
        f'<td style="padding:6px 0;text-align:right;color:{INK}">{gbp(i["qty"] * i["price"])}</td></tr>'
        for i in order["items"])
    body = f"""<!doctype html><html><body style="margin:0;background:{CREAM};font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CREAM};padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#FBF9F4;border:2px solid {INK}">
  <tr><td style="background:{GREEN};padding:24px 24px 22px">
    <div style="font-size:26px;font-weight:900;letter-spacing:-.5px;color:{CREAM}">JULIUS.</div>
    <div style="margin-top:18px;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#8FA596">Your order number</div>
    <div style="font-size:56px;line-height:1;font-weight:900;color:{CREAM};margin-top:6px">{e(order['code'])}</div>
    <div style="margin-top:10px;color:#C9D3CB;font-size:15px">Thanks {e(order['name'])}, you're paid and booked in.</div>
  </td></tr>
  <tr><td style="padding:22px 24px 8px">
    <div style="font-size:22px;font-weight:800;color:{INK}">Collect {e(when)}</div>
    <div style="margin-top:4px;font-size:14px;color:{MUTED}">Kiosk 4, Reuters Plaza, Canary Wharf. Skip the queue and go straight to the hand-off point.</div>
  </td></tr>
  <tr><td style="padding:8px 24px 4px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:15px;border-top:1px solid #D9D3C4">
      {rows}
      <tr><td style="padding:10px 0 0;border-top:1.5px solid {INK};font-weight:700;color:{INK}">Total paid</td>
          <td style="padding:10px 0 0;border-top:1.5px solid {INK};text-align:right;font-weight:700;color:{INK}">{gbp(order['total'])}</td></tr>
    </table>
  </td></tr>
  <tr><td style="padding:22px 24px 26px">
    <a href="{e(order_url)}" style="display:inline-block;background:{GREEN};color:{CREAM};text-decoration:none;font-weight:700;font-size:14px;padding:14px 20px">Track your order</a>
  </td></tr>
</table>
<div style="max-width:520px;margin-top:14px;font-size:12px;color:{MUTED}">JULIUS — Caesar, done right. · eatjulius.com</div>
</td></tr></table></body></html>"""
    return subject, text, body


def send(to, order, order_url):
    subject, text, body = build(order, order_url)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("JULIUS", MAIL_FROM))
    msg["To"] = to
    msg["Reply-To"] = MAIL_REPLY_TO
    msg["Message-ID"] = make_msgid(domain=MAIL_FROM.split("@")[-1])
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=20) as s:
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            if SMTP_PORT == 587:
                s.starttls(context=ssl.create_default_context())
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)


def send_async(to, order, order_url):
    """Fire-and-forget so a slow mail server never delays the webhook or page."""
    if not SMTP_HOST:
        print(f"[mail] SMTP not configured, skipped confirmation for {order['code']}", flush=True)
        return

    def run():
        try:
            send(to, order, order_url)
            print(f"[mail] sent {order['code']} confirmation", flush=True)
        except Exception as e:  # never let mail break ordering
            print(f"[mail] failed for {order['code']}: {e!r}", flush=True)

    threading.Thread(target=run, daemon=True).start()
