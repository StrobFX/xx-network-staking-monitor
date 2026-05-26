# xx Network Staking Monitor

A local monitoring tool for xx Network nominators and validators. It builds a
Markdown report and CSV exports, checks current on-chain commissions and
capacity, tracks commission changes between runs, and can send email only when
action may be required.

## License

This project is released under the [MIT License](LICENSE).

## Support This Project

If you find this tool useful and would like to support its development, tips
in XX are welcome at the public on-chain identity **StrobFX Tips**:

```text
6WrCReM86qyZdKwcGHmGxwQ6wxz5NTowNkUe5zk59UVWUbjF
```

You can verify the `StrobFX Tips` identity in the xx Network explorer before
sending a tip. This is the project's intentionally public tip address.

## Features

- Average staking rewards per account over the last 30 eras by default.
- Total average rewards across all configured accounts in XX/day, USD/day, and
  CAD/day.
- Current nominations and validator commissions read from the live chain RPC.
- Active-era exposure: total stake, active nominators, and the validator(s)
  actually carrying each nominator's stake.
- Alerts for commission increases, commission above a configured maximum,
  inactive targets, and high-commission assignments still active
  from an earlier nomination list.
- Low-commission validator candidate table using currently available indexed
  history.
- Optional Gmail notification and Windows Task Scheduler automation.

## Data Sources

- Historical reward data and indexed identity/history:
  `https://indexer.xx.network/v1/graphql`
- Current nominations, commissions, and active validator exposure:
  `https://xxnetwork-rpc.n.dwellir.com`
- Automatic official fallback RPC if the primary RPC fails:
  `wss://rpc.xx.network`
- XX market price in USD and CAD:
  CoinGecko simple price API (`xxcoin`)

Current commission values are deliberately obtained from on-chain
`Staking.Validators`, rather than relying on `validator_stats`, because indexed
statistics can lag behind the current chain state.

## Requirements

- Python 3.10 or newer. The Python report itself uses only the standard
  library.
- Windows PowerShell for the optional email-configuration and scheduled-run
  helper scripts.
- A Gmail account with two-step verification and a Google app password is
  required only if you want email alerts.

## Add Your Accounts

No wallet addresses are included in this repository.

1. Rename or duplicate `xx_staking_accounts.example.json` as
   `xx_staking_accounts.json`.
2. Replace the example entries with your own account labels and xx Network
   addresses.
3. Add or remove objects as needed.

Example:

```json
[
  {
    "label": "main-wallet",
    "address": "YOUR_XX_NETWORK_ADDRESS"
  },
  {
    "label": "validator-stash",
    "address": "ANOTHER_XX_NETWORK_ADDRESS"
  }
]
```

The real `xx_staking_accounts.json` file is ignored by Git by default.

## Generate A Report

On any platform:

```bash
python xx_staking_report.py
```

On Windows, you can also double-click `run_xx_staking_report.bat`.

Useful options:

```bash
python xx_staking_report.py --days 90 --max-commission 22
python xx_staking_report.py --rpc-url https://your-rpc.example --fallback-rpc-url wss://rpc.xx.network
```

Defaults:

- Reward average window: `30` eras.
- Maximum commission accepted without alert: `<= 22%`; alert above `22%`.
- Candidate current commission: `<= 18%`.
- Candidate maximum observed indexed commission: `<= 25%`.

The script writes:

- `xx_staking_output/report.md`
- `xx_staking_output/rewards_summary.csv`
- `xx_staking_output/validator_commissions.csv`
- `xx_staking_output/nomination_candidates.csv`
- `xx_staking_output/monitor_state.json`

`monitor_state.json` is used to detect a commission increase between
successive runs.

The script uses the public HTTP RPC first. If it fails during a run, the
script automatically switches to the official `wss://rpc.xx.network`
endpoint and records the fallback in `report.md`. The WebSocket support is
implemented with Python's standard library; no additional package is needed.
Use `--rpc-url` or `--fallback-rpc-url` to supply different endpoints.

## Alerts

The report separates current targets from active-era assignments. This matters
because changing nominations does not immediately remove an earlier assignment
from the current era.

An alert is raised when:

- a current nomination target increases its commission since the previous run;
- a current target has commission above the configured maximum;
- a current target no longer has a live `Staking.Validators` entry;
- stake remains actively assigned in the current era to a validator whose
  commission is above the configured maximum.

## Capacity And The 256-Nominator Limit

xx Network rewards only the top 256 nominators on a validator. Commission alone
is therefore not sufficient when choosing nomination targets. The report shows
each relevant validator's active-era total stake and active nominator count,
as well as the active stake assignment for each configured account.

The candidate table filters for validators that are active now, are not your
own validators, currently charge no more than 18%, have at least 60 indexed
historical eras, and have no indexed commission observation above 25%.
Bootnodes are excluded.

The explorer history may be incomplete. Candidate history is evidence from the
indexed data available, not a guarantee about unindexed eras.

## Email Notifications On Windows

Email is optional and is sent only for alerts unless you request a test
message. Gmail requires two-step verification and a Google app password.
Google's official instructions are available at:
<https://support.google.com/accounts/answer/185833>.

Create the encrypted local email configuration:

```powershell
.\configure_xx_staking_email.ps1 -Sender "your.address@gmail.com"
```

To send alerts to a different address:

```powershell
.\configure_xx_staking_email.ps1 -Sender "sender@gmail.com" -Recipient "alerts@example.com"
```

The app password is encrypted for the current Windows user and stored outside
this repository under `%LOCALAPPDATA%\xx_staking_report\email_config.json`.
It is not written to the project files.

Send a test email:

```powershell
.\run_xx_staking_monitor.ps1 -TestEmail
```

Run normally, sending email only if an alert exists:

```powershell
.\run_xx_staking_monitor.ps1
```

If Python cannot be discovered automatically, supply its path:

```powershell
.\run_xx_staking_monitor.ps1 -PythonPath "C:\Path\To\python.exe"
```

## Daily Scheduling On Windows 11

Create a task in **Task Scheduler**:

1. Click **Create Task...** and name it `xx Network staking monitor`.
2. On **Triggers**, add a **Daily** trigger at your preferred time.
3. On **Actions**, choose **Start a program**.
4. Set **Program/script** to `powershell.exe`.
5. Set **Add arguments** to:

   ```text
   -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Path\To\xx-staking-monitor\run_xx_staking_monitor.ps1"
   ```

6. Set **Start in** to the repository folder.
7. Optionally enable **Wake the computer to run this task**.
8. Enable **Run task as soon as possible after a scheduled start is missed**.

Run the scheduled task under the same Windows user that created the encrypted
Gmail configuration.

## Privacy Notes

- Do not commit `xx_staking_accounts.json`.
- Do not commit generated report output if it reveals balances or nomination
  activity.
- Never store a Gmail password or app password in the repository.
