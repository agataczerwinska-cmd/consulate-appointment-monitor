# Consulate appointment monitor

A small Python tool that watches a Microsoft Bookings page for one specific
service and sends a phone notification the moment a new appointment slot opens.

I built it because notarial appointments at the Colombian consulate in Berlin
were released rarely and taken within minutes. Checking the page by hand
several times a day was not working, so I automated the check.

**Status:** finished. It did its job (a slot was found and booked) and is kept
here as a reference project.

## How it works

1. Opens the public booking page in a headless Chromium browser (Playwright).
2. Selects the service by name. The calendar stays disabled until a service is
   picked.
3. Reads which calendar days are bookable for the next six months, then opens
   each open day and collects the available times.
4. Compares the result with the previous run, stored in `state.json`.
5. If something new appeared, sends a push notification through
   [ntfy.sh](https://ntfy.sh) with a link to the booking page.

It only reads availability. It does not fill in the form or book anything.

## Stack

- Python 3, Playwright (Chromium), requests
- ntfy.sh for push notifications
- Scheduled runs with cron or GitHub Actions

## Problems I solved along the way

- **Empty calendar.** The first version read the calendar before choosing a
  service, so every day looked closed. Fixed by selecting the service first.
- **Missed open days.** The page builds calendar days as
  `div[role="button"]` elements, not `<button>` tags, and marks closed days
  with `aria-disabled="true"`. I found this by inspecting the page in DevTools
  and rewrote the selector to match the real markup.
- **Slots further out than expected.** New slots were released more than three
  months ahead, so I extended the look-ahead window from three to six months.
- **False alarms on the first run.** The script now distinguishes "no state
  file yet" from "previous run found nothing", so the baseline run stays quiet.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium

export NTFY_TOPIC=your-private-topic
python3 watch_booking.py --once --debug   # single check, saves a screenshot
python3 watch_booking.py                  # check every 5 minutes
```

To watch a different service, change `SERVICE_NAME` at the top of
`watch_booking.py`.

## Configuration and privacy

The ntfy topic works like a password, so it is read from the `NTFY_TOPIC`
environment variable (a repository secret when run on GitHub Actions) and is
never stored in the code. Runtime files (`state.json`, `log.txt`, screenshots,
debug HTML) are excluded through `.gitignore`.

## Responsible use

The script checks a public page at a modest interval and takes no action on
it. If you adapt it, keep the interval reasonable and respect the terms of the
site you are checking.
