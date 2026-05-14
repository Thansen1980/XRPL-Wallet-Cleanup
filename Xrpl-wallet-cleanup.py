#!/usr/bin/env python3
"""
XRPL Wallet Cleanup
═══════════════════
Scans all trustlines on a wallet and removes every token NOT in the EXCLUDE list.

For each token to remove, the script will:
  1. Check the DEX (BookOffers) for existing buy-side bids
  2. If bids exist  → OfferCreate (ImmediateOrCancel + Sell) to fill immediately
  3. If balance remains after DEX (or no bids existed) → Payment back to issuer
     Fallback chain: issuer → issuer with destination_tag=0 → XRPL black hole
  4. Remove the trustline (TrustSet limit = 0)

Modes
  TEST = True   Dry-run — fetches live data, prints a detailed plan, no transactions sent.
  TEST = False  Live execution, one token at a time with a 5-second countdown before start.

Requirements
  pip install xrpl-py
"""

import time
from decimal import Decimal

from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import AccountLines
from xrpl.models.requests import BookOffers as BookOffersReq
from xrpl.models.transactions import OfferCreate, Payment, TrustSet
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import XRP as XRPCurrency, IssuedCurrency
from xrpl.transaction import autofill, sign, submit_and_wait

# ══════════════════════════════════════════════════════════════════════════════
#  ⚙  CONFIG — edit these values
# ══════════════════════════════════════════════════════════════════════════════

WALLET_SEED = "sXXXXXXXXXXXXXXXXXXXXXXXXXX"   # ← your wallet seed (family seed)
XRPL_NODE   = "https://s1.ripple.com:51234"       # public node or your own
TEST        = True    # True = dry-run · False = live (submits real transactions)

# Tokens to keep — everything else will be removed.
# Format: "CURRENCY-rISSUER_ADDRESS"
# Works with both 3-char tickers (XPM) and longer names (PIXELVERSE, RLUSD, …)
EXCLUDE = [
    "XPM-rXPMxBeefHGxx2K7g5qmmWq3gFsgawkoa",
    # "MYTOKEN-rISSUERADDRESSHERE",
]

# Seconds to pause between tokens (avoids sequence errors and rate limits)
PAUSE_BETWEEN_TX = 3

# ══════════════════════════════════════════════════════════════════════════════
#  Internal constants
# ══════════════════════════════════════════════════════════════════════════════
TF_IMMEDIATE_OR_CANCEL = 0x00020000
TF_SELL                = 0x00080000
# TrustSet flags (TS_ prefix to distinguish from OfferCreate flags)
TS_SET_NO_RIPPLE       = 0x00020000  # Match Xaman: set NoRipple on our side
TS_CLEAR_FREEZE        = 0x00200000  # Clear our side of any freeze
BURN_ADDRESS           = "rrrrrrrrrrrrrrrrrrrrhoLvTp"  # XRPL black hole (no one holds the key)


# ─────────────────────────────────────────────────────────────────────────────
#  Currency helpers
# ─────────────────────────────────────────────────────────────────────────────

def hex_to_ascii(code: str) -> str:
    """
    XRPL stores currency codes longer than 3 characters as 40-char hex on the ledger.
    This function attempts to decode them back to a human-readable name.

    Example: "5049584556455253450000000000000000000000" → "PIXELVERSE"

    Returns the original hex string if decoding fails or yields non-printable bytes.
    """
    if len(code) != 40:
        return code  # standard 3-char code — return as-is
    try:
        raw     = bytes.fromhex(code).rstrip(b"\x00")
        decoded = raw.decode("ascii")
        if decoded and decoded.isprintable():
            return decoded
    except Exception:
        pass
    return code


def display_currency(code: str) -> str:
    """Returns 'NAME  (hex)' when the hex can be decoded, otherwise just the hex."""
    if len(code) == 40:
        name = hex_to_ascii(code)
        if name != code:
            return f"{name}  ({code})"
    return code


# ─────────────────────────────────────────────────────────────────────────────
#  Exclude list helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_exclude(entries: list[str]) -> set[tuple[str, str]]:
    """
    Converts ['CUR-rISSUER', …] into a set of (CURRENCY_UPPER, issuer) tuples.
    Splits on the first '-r' since XRPL issuer addresses always start with 'r'.
    Stores human-readable names — hex_to_ascii() is used at comparison time.
    """
    result: set[tuple[str, str]] = set()
    for entry in entries:
        idx = entry.find("-r")
        if idx == -1:
            print(f"[WARN] Cannot parse exclude entry, skipping: {entry}")
            continue
        result.add((entry[:idx].upper(), entry[idx + 1:]))
    return result


def is_excluded(currency: str, issuer: str, exclude_set: set) -> bool:
    """
    Compares against both the raw ledger code and the decoded ASCII name.
    Necessary because account_lines returns hex for tokens with names longer than
    3 characters, while EXCLUDE uses human-readable names (PIXELVERSE, RLUSD, …).
    """
    readable = hex_to_ascii(currency)
    return (readable.upper(), issuer) in exclude_set or (currency.upper(), issuer) in exclude_set


# ─────────────────────────────────────────────────────────────────────────────
#  XRPL data fetching
# ─────────────────────────────────────────────────────────────────────────────

def get_all_trustlines(client: JsonRpcClient, address: str) -> list[dict]:
    """Fetches all trustlines for an address, paginating via marker."""
    lines: list[dict] = []
    marker = None
    while True:
        req = AccountLines(account=address, ledger_index="validated", marker=marker)
        resp = client.request(req).result
        lines.extend(resp.get("lines", []))
        marker = resp.get("marker")
        if not marker:
            break
    return lines


def check_dex(client: JsonRpcClient, currency: str, issuer: str) -> dict:
    """
    Looks up the order book for existing bids (people willing to BUY our token).

    We query the book where:
      taker_pays = our token  (the taker pays with our token)
      taker_gets = XRP        (and receives XRP)
    i.e. offers created by accounts willing to GIVE XRP and RECEIVE our token.

    Returns: {has_bids, count, best_xrp_rate}
    """
    try:
        req = BookOffersReq(
            taker_pays=IssuedCurrency(currency=currency, issuer=issuer),
            taker_gets=XRPCurrency(),
            limit=10,
        )
        offers = client.request(req).result.get("offers", [])

        if not offers:
            return {"has_bids": False, "count": 0, "best_xrp_rate": None}

        # Best rate from the top offer
        best_rate: str | None = None
        try:
            b          = offers[0]
            xrp_drops  = Decimal(str(b["taker_gets"]))
            tok_amount = Decimal(str(b["taker_pays"]["value"]))
            if tok_amount > 0:
                rate      = xrp_drops / Decimal("1000000") / tok_amount
                best_rate = f"{rate:.8f} XRP/token"
        except Exception:
            best_rate = "unknown rate"

        return {"has_bids": True, "count": len(offers), "best_xrp_rate": best_rate}

    except Exception as exc:
        print(f"    [WARN] DEX lookup failed: {exc}")
        return {"has_bids": False, "count": 0, "best_xrp_rate": None}


# ─────────────────────────────────────────────────────────────────────────────
#  Transaction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _engine_result(result: dict) -> tuple[str, str]:
    """
    xrpl-py's submit_and_wait can return either:
      submit response   : result["engine_result"] + result["engine_result_message"]
      tx-lookup response: result["meta"]["TransactionResult"]  (newer xrpl-py versions)
    This function checks both and returns (code, message).
    """
    engine = result.get("engine_result", "")
    msg    = result.get("engine_result_message", "")
    if not engine:
        engine = result.get("meta", {}).get("TransactionResult", "")
    return engine, msg


def dex_sell(client: JsonRpcClient, wallet: Wallet,
             currency: str, issuer: str, balance: str) -> str:
    """
    Attempts to sell all tokens on the DEX using ImmediateOrCancel + Sell flags.
      taker_pays = 1 drop XRP  (accept any price)
      taker_gets = full balance

    submit_and_wait raises an exception for tec-class results:
      tecKILLED = IOC order got no immediate fills → returns original balance.
    All other errors are logged and original balance returned as fallback.
    """
    tx = OfferCreate(
        account=wallet.classic_address,
        taker_pays="1",  # 1 drop XRP = accept any price
        taker_gets=IssuedCurrencyAmount(
            currency=currency,
            issuer=issuer,
            value=balance,
        ),
        flags=TF_IMMEDIATE_OR_CANCEL | TF_SELL,
    )
    tx  = autofill(tx, client)
    stx = sign(tx, wallet)

    try:
        result      = submit_and_wait(stx, client).result
        engine, msg = _engine_result(result)
        print(f"    OfferCreate → {engine}  {msg}")
    except Exception as exc:
        err = str(exc)
        if "tecKILLED" in err:
            print("    OfferCreate → tecKILLED  (no immediate fills — falling back to send)")
        else:
            print(f"    OfferCreate → ERROR: {exc}")
        return balance  # return original balance; send_to_issuer will handle the rest

    # Give the ledger a moment and fetch the updated balance
    time.sleep(1)
    for line in get_all_trustlines(client, wallet.classic_address):
        if line["account"] == issuer and line["currency"].upper() == currency.upper():
            return line["balance"]
    return "0"


def _try_payment(client: JsonRpcClient, wallet: Wallet,
                 currency: str, issuer: str, balance: str,
                 destination: str, dest_tag: int | None = None) -> bool:
    """Internal helper: attempts a single Payment transaction. Returns True on success."""
    kwargs: dict = dict(
        account=wallet.classic_address,
        destination=destination,
        amount=IssuedCurrencyAmount(currency=currency, issuer=issuer, value=balance),
    )
    if dest_tag is not None:
        kwargs["destination_tag"] = dest_tag
    try:
        tx          = Payment(**kwargs)
        tx          = autofill(tx, client)
        stx         = sign(tx, wallet)
        result      = submit_and_wait(stx, client).result
        engine, msg = _engine_result(result)
        print(f"    Payment → {destination[:24]}...  {engine}  {msg}")
        return engine == "tesSUCCESS"
    except Exception as exc:
        print(f"    Payment → {destination[:24]}...  ERROR: {exc}")
        return False


def send_to_issuer(client: JsonRpcClient, wallet: Wallet,
                   currency: str, issuer: str, balance: str) -> bool:
    """
    Sends tokens away via a fallback chain:
      1. Payment to issuer
      2. Payment to issuer with destination_tag=0  (handles RequireDestTag flag)
      3. Payment to XRPL black hole               (when issuer account is deleted/unreachable)
    Returns True if any attempt succeeded.
    """
    attempts = [
        (issuer,       None, "issuer"),
        (issuer,       0,    "issuer + tag=0"),
        (BURN_ADDRESS, None, "black hole"),
    ]
    for dest, tag, label in attempts:
        print(f"    Trying: {label}...")
        if _try_payment(client, wallet, currency, issuer, balance, dest, tag):
            return True
        time.sleep(1)
    print("    [ERROR] All send attempts failed.")
    return False


def remove_trustline(client: JsonRpcClient, wallet: Wallet,
                     currency: str, issuer: str) -> bool:
    """Removes the trustline by setting the limit amount to 0."""
    try:
        tx = TrustSet(
            account=wallet.classic_address,
            limit_amount=IssuedCurrencyAmount(
                currency=currency,
                issuer=issuer,
                value="0",
            ),
            flags=TS_SET_NO_RIPPLE | TS_CLEAR_FREEZE,
        )
        tx          = autofill(tx, client)
        stx         = sign(tx, wallet)
        result      = submit_and_wait(stx, client).result
        engine, msg = _engine_result(result)
        print(f"    TrustSet (remove) → {engine}  {msg}")
        return engine == "tesSUCCESS"
    except Exception as exc:
        print(f"    TrustSet (remove) → ERROR: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Output formatting
# ─────────────────────────────────────────────────────────────────────────────

def line_single(char: str = "─", width: int = 62) -> None:
    print(char * width)

def line_double(width: int = 62) -> None:
    print("═" * width)


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    mode_label = "TEST (dry-run — no transactions)" if TEST else "⚠️  PRODUCTION (live)"

    line_double()
    print(f"  XRPL Wallet Cleanup  ·  {mode_label}")
    line_double()
    print()

    client = JsonRpcClient(XRPL_NODE)
    wallet = Wallet.from_seed(WALLET_SEED)
    addr   = wallet.classic_address
    excl   = parse_exclude(EXCLUDE)

    print(f"  Wallet  : {addr}")
    print(f"  Node    : {XRPL_NODE}")
    print()

    # ── Fetch all trustlines ──────────────────────────────────────────────────
    print("Fetching trustlines...", end=" ", flush=True)
    all_lines = get_all_trustlines(client, addr)
    print(f"{len(all_lines)} found.\n")

    to_skip: list[dict]    = []
    to_process: list[dict] = []

    for line in all_lines:
        if is_excluded(line["currency"], line["account"], excl):
            to_skip.append(line)
        else:
            to_process.append(line)

    # ── Show excluded tokens ──────────────────────────────────────────────────
    print(f"EXCLUDED ({len(to_skip)}) — will not be touched:")
    if to_skip:
        for ln in to_skip:
            bal_str  = f"{ln['balance']:>20}"
            name_str = display_currency(ln["currency"])
            print(f"  ✓  {name_str:<55}  bal: {bal_str}")
    else:
        print("  (none)")
    print()

    # ── Tokens to remove ─────────────────────────────────────────────────────
    total = len(to_process)
    print(f"TO REMOVE ({total}):")

    if not total:
        print("  (none — nothing to do)\n")
        return

    # ── TEST mode: analyse all tokens and print plan, then stop ──────────────
    if TEST:
        for ln in to_process:
            cur     = ln["currency"]
            iss     = ln["account"]
            bal     = ln["balance"]
            bal_dec = Decimal(bal)

            dex = check_dex(client, cur, iss) if bal_dec > 0 else \
                  {"has_bids": False, "count": 0, "best_xrp_rate": None}

            steps: list[str] = []
            if bal_dec > 0:
                if dex["has_bids"]:
                    rate_info = f" · best rate: {dex['best_xrp_rate']}" if dex["best_xrp_rate"] else ""
                    steps.append(f"DEX sell  ({dex['count']} bid(s){rate_info})")
                    steps.append("Send remainder to issuer (if any left)")
                else:
                    steps.append("Send all tokens to issuer  (no DEX bids)")
            elif bal_dec < 0:
                steps.append(f"Negative balance ({bal}) — skip send, attempt trustline removal")
            else:
                steps.append("Balance = 0 — no send needed")
            steps.append("Remove trustline  (TrustSet limit=0)")

            # Fetch raw line for diagnostics (esp. for zero-balance stuck lines)
            raw = next(
                (l for l in all_lines
                 if l["account"] == iss and l["currency"].upper() == cur.upper()),
                None,
            )
            limit_peer = raw.get("limit_peer", "0") if raw else "?"
            peer_auth  = raw.get("peer_authorized", False) if raw else "?"

            line_single()
            print(f"  TOKEN      : {display_currency(cur)}")
            print(f"  ISSUER     : {iss}")
            print(f"  BALANCE    : {bal}")
            bids_txt = f"{dex['count']} bid(s)" if dex["has_bids"] else "none"
            print(f"  DEX        : {bids_txt}")
            if bal_dec == 0:
                no_ripple      = raw.get("no_ripple", False) if raw else "?"
                no_ripple_peer = raw.get("no_ripple_peer", False) if raw else "?"
                zombie = (limit_peer != "0" or peer_auth or no_ripple_peer)
                stuck_flag = " ← ZOMBIE (peer flag — cannot remove)" if zombie else " ← ok"
                print(f"  LIMIT_PEER    : {limit_peer}")
                print(f"  PEER_AUTH     : {peer_auth}")
                print(f"  NO_RIPPLE     : {no_ripple}")
                print(f"  NO_RIPPLE_PEER: {no_ripple_peer}{stuck_flag}")
            print(f"  PLAN       :")
            for i, step in enumerate(steps, 1):
                print(f"             {i}. {step}")

        line_single()
        line_double()
        print()
        print(f"  [TEST] {total} token(s) would be removed.")
        print("  Set TEST = False to execute.\n")
        return

    # ══════════════════════════════════════════════════════════════════════════
    #  PRODUCTION — analyse and execute one token at a time
    # ══════════════════════════════════════════════════════════════════════════

    # Countdown so you can still Ctrl+C before anything is submitted
    print()
    print("  Starting in ", end="", flush=True)
    for sec in range(5, 0, -1):
        print(f"{sec}...", end=" ", flush=True)
        time.sleep(1)
    print("GO!\n")

    summary: list[tuple[str, str, str]] = []  # (currency, issuer, status)
    done = 0

    for ln in to_process:
        cur     = ln["currency"]
        iss     = ln["account"]
        bal     = ln["balance"]
        bal_dec = Decimal(bal)
        done   += 1

        line_single()
        print(f"  [{done}/{total}]  {display_currency(cur)}  /  {iss}")
        print(f"  BALANCE : {bal}")

        # Live DEX check
        dex = check_dex(client, cur, iss) if bal_dec > 0 else \
              {"has_bids": False, "count": 0, "best_xrp_rate": None}

        bids_txt = f"{dex['count']} bid(s)" if dex["has_bids"] else "none"
        print(f"  DEX     : {bids_txt}")

        send_ok = True

        # ── Send / sell ───────────────────────────────────────────────────────
        if bal_dec > 0:
            if dex["has_bids"]:
                print("  → Attempting DEX sell...")
                remaining = dex_sell(client, wallet, cur, iss, bal)
                rem_dec   = Decimal(remaining)
                if rem_dec > 0:
                    print(f"  → Partial fill. Remaining: {remaining} — sending to issuer...")
                    send_ok = send_to_issuer(client, wallet, cur, iss, remaining)
                    if not send_ok:
                        print("  [ERROR] All send attempts failed!")
                else:
                    print("  → Fully filled on DEX.")
            else:
                print("  → Sending to issuer (no DEX bids)...")
                send_ok = send_to_issuer(client, wallet, cur, iss, bal)
                if not send_ok:
                    print("  [ERROR] All send attempts failed!")

        elif bal_dec < 0:
            print(f"  → Negative balance ({bal}). Skipping send.")
        else:
            print("  → Balance is 0. Skipping send.")

        # ── Remove trustline ─────────────────────────────────────────────────
        if send_ok:
            print("  → Removing trustline...")
            tl_ok = remove_trustline(client, wallet, cur, iss)

            if tl_ok:
                # Verify the line is actually gone from the ledger
                time.sleep(1)
                still_there = any(
                    l["account"] == iss and l["currency"].upper() == cur.upper()
                    for l in get_all_trustlines(client, addr)
                )
                if still_there:
                    print("  [WARN] tesSUCCESS but trustline still exists (ledger-level zombie).")
                    status = "✗ STUCK_ZOMBIE"
                else:
                    status = "✓ CLEANED"
            else:
                status = "✗ TRUSTLINE_ERROR"
        else:
            print("  [SKIP] Skipping trustline removal due to send failure.")
            status = "✗ SEND_ERROR"

        summary.append((cur, iss, status))
        print(f"  STATUS  : {status}")

        if done < total:
            print(f"  (pausing {PAUSE_BETWEEN_TX}s...)", flush=True)
            time.sleep(PAUSE_BETWEEN_TX)

    # ── Final summary ─────────────────────────────────────────────────────────
    line_double()
    print("\nSUMMARY\n")

    cleaned = [s for s in summary if s[2] == "✓ CLEANED"]
    zombies = [s for s in summary if s[2] == "✗ STUCK_ZOMBIE"]
    errors  = [s for s in summary if s[2] not in ("✓ CLEANED", "✗ STUCK_ZOMBIE")]

    if cleaned:
        print(f"  ✓ CLEANED ({len(cleaned)})")
        for cur, iss, _ in cleaned:
            print(f"      {display_currency(cur)[:50]:<50}  {iss}")
        print()

    if errors:
        print(f"  ✗ ERRORS ({len(errors)})")
        for cur, iss, status in errors:
            print(f"      [{status}]  {display_currency(cur)[:40]:<40}  {iss}")
        print()

    if zombies:
        print(f"  ✗ STUCK_ZOMBIE ({len(zombies)})  — cannot be removed by holder")
        print("     The issuer has lsfNoRipple set on their side of the trust line.")
        print("     XRPL protocol requires flags=0 before a RippleState object can be deleted.")
        print("     The issuer must clear their side — holders have no recourse.")
        print("     Each zombie costs 2 XRP in owner reserve (permanently locked).")
        print()
        # Group by issuer for readability
        issuers: dict[str, list[str]] = {}
        for cur, iss, _ in zombies:
            issuers.setdefault(iss, []).append(display_currency(cur))
        for iss, tokens in issuers.items():
            print(f"      Issuer: {iss}  ({len(tokens)} token(s))")
            for t in tokens:
                print(f"        • {t}")
        print()

    print(f"  Total processed : {len(summary)}")
    print(f"  Cleaned         : {len(cleaned)}")
    print(f"  Zombie (stuck)  : {len(zombies)}  (~{len(zombies) * 2} XRP reserve locked)")
    print(f"  Other errors    : {len(errors)}")
    print()


if __name__ == "__main__":
    main()
