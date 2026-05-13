# XRPL Wallet Cleanup

A Python script that removes unwanted token trustlines from an XRPL wallet — cleanly and automatically.

For each token not in your keep-list, the script will:

1. **Check the DEX** for existing buy-side bids (BookOffers)
2. **Sell on DEX** if bids exist (ImmediateOrCancel offer — no resting orders left behind)
3. **Send to issuer** if no DEX bids, or for any remaining balance after a partial fill
   - Fallback chain: issuer → issuer with `destination_tag=0` → XRPL black hole
4. **Remove the trustline** (TrustSet limit = 0)

Tested on a wallet with 430 trustlines.

---

## Features

- **Test mode** — fetches live data and prints a full per-token action plan without submitting any transactions
- **Hex currency decoding** — tokens like `PIXELVERSE` or `RLUSD` are stored as 40-char hex on the ledger; the script decodes and displays them as human-readable names automatically
- **Exclude list** — simple `"CURRENCY-rISSUER"` format; works with both 3-char tickers and longer names
- **Progress counter** — shows `[47/424]` as it works through your wallet
- **5-second countdown** before live execution so you can still Ctrl+C
- **Safe to interrupt** — the script holds no state; tokens already cleaned stay cleaned, just re-run for the rest
- **Robust engine result handling** — compatible with both older and newer versions of xrpl-py (checks both `engine_result` and `meta.TransactionResult`)

---

## Requirements

```
Python 3.11+
xrpl-py
```

Install:

```bash
pip install xrpl-py
```

---

## Setup

1. Clone or download `xrpl_wallet_cleanup.py`
2. Open the file and edit the **CONFIG** section at the top:

```python
WALLET_SEED = "sXXXXXXXXXXXXXXXXXXXXXXXXXX"   # your family seed
XRPL_NODE   = "https://s1.ripple.com:51234"       # or your own node
TEST        = True                                 # start here

EXCLUDE = [
    "XPM-rXPMxBeefHGxx2K7g5qmmWq3gFsgawkoa",
    "RLUSD-rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",
    # add any other tokens you want to keep
]
```

3. Run in test mode first:

```bash
python xrpl_wallet_cleanup.py
```

4. Review the printed plan, then set `TEST = False` and run again to execute.

---

## Exclude list format

Each entry is `"CURRENCY-rISSUER_ADDRESS"`. The currency name is whatever you see in your wallet UI — the script handles the hex encoding internally.

```python
EXCLUDE = [
    "XPM-rXPMxBeefHGxx2K7g5qmmWq3gFsgawkoa",        # 3-char ticker
    "RLUSD-rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",     # longer name
    "PIXELVERSE-rPffgtw9ufS6pT4Wa23jVf1g3nw1GL5D1b", # works too
]
```

---

## Example output

**Test mode:**
```
══════════════════════════════════════════════════════════════
  XRPL Wallet Cleanup  ·  TEST (dry-run — no transactions)
══════════════════════════════════════════════════════════════

  Wallet  : rYourWalletAddressHere
  Node    : https://s1.ripple.com:51234

Fetching trustlines... 430 found.

EXCLUDED (2) — will not be touched:
  ✓  XPM                                                      bal:    1292.445173029731
  ✓  RLUSD  (524C555344000000000000000000000000000000)        bal:     17.0371509349662

TO REMOVE (428):
──────────────────────────────────────────────────────────────
  TOKEN  : ARMY  (41524D5900000000000000000000000000000000)
  ISSUER : rGG3wQ4kUzd7Jnmk1n5NWPZjjut62kCBfC
  BALANCE: 0.095355255905
  DEX    : 10 bid(s)
  PLAN   :
           1. DEX sell  (10 bid(s) · best rate: 0.00000412 XRP/token)
           2. Send remainder to issuer (if any left)
           3. Remove trustline  (TrustSet limit=0)
```

**Live mode:**
```
══════════════════════════════════════════════════════════════
  XRPL Wallet Cleanup  ·  ⚠️  PRODUCTION (live)
══════════════════════════════════════════════════════════════

  Starting in 5... 4... 3... 2... 1... GO!

──────────────────────────────────────────────────────────────
  [1/428]  ARMY  (41524D59...)  /  rGG3wQ4kUzd7...
  BALANCE : 0.095355255905
  DEX     : 10 bid(s)
  → Attempting DEX sell...
    OfferCreate → tesSUCCESS
  → Fully filled on DEX.
  → Removing trustline...
    TrustSet (remove) → tesSUCCESS
  STATUS  : ✓ CLEANED
  (pausing 3s...)
```

---

## Known limitations

**`✗ SEND_ERROR` tokens** — some tokens cannot be returned to their issuer because:
- The issuer account has been deleted (`tecNO_DST`)
- The token path is dry — issuer no longer accepts it back (`tecPATH_DRY`)
- The token is frozen by the issuer

In these cases the script tries all three destinations (issuer, issuer+tag, black hole) and skips trustline removal if all fail. There is no XRPL-level workaround for a fully frozen or path-dry token with a non-zero balance — these are genuinely stuck and will remain in your wallet.

---

## Security note

Your seed is hardcoded in the script. Do not commit the file with a real seed in it. Consider loading it from an environment variable instead:

```python
import os
WALLET_SEED = os.environ["WALLET_SEED"]
```

---

## License

MIT
