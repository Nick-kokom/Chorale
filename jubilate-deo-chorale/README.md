# Jubilate Deo Chorale

*Sing with faith. Serve through music.*

A Catholic parish liturgical choir management system.
Built with **HTML, CSS, Python (Flask) and SQLite**, plus a little optional vanilla JavaScript.

## What exists right now (Phase 1: foundation)

- Shared design system (burgundy, white, charcoal, warm gold), responsive on desktop and mobile
- First-run **administrator setup** page
- **Sign in / sign out** with hashed passwords and CSRF protection
- Protected **dashboard** shell with a mobile navigation drawer
- SQLite database created automatically on first run
- Logo placeholder that is replaced automatically when you add your own logo

Not built yet (planned): member management, announcements, uploads, offline functionality.

## Project structure

```
jubilate-deo-chorale/
├── app.py                 Flask application (routes, database, login)
├── requirements.txt       Python packages
├── .env.example           Template for your private settings
├── templates/
│   ├── base.html          Shared layout, logo and flash-message macros
│   ├── login.html
│   ├── setup_admin.html
│   └── dashboard.html
├── static/
│   ├── css/style.css      Design tokens and all styling
│   ├── js/app.js          Small interactions
│   └── images/            Put logo.png here
├── instance/              SQLite database lives here (created automatically)
└── .vscode/launch.json    One-click debugging in VS Code
```

---

## Setup on Windows with VS Code

### 1. Install the tools (one time)

1. **Python 3.10 or newer**: download from <https://www.python.org/downloads/>.
   On the first installer screen, **tick "Add python.exe to PATH"**, then click *Install Now*.
2. **Visual Studio Code**: <https://code.visualstudio.com/>.
3. In VS Code, open the Extensions panel (`Ctrl+Shift+X`) and install **Python** (by Microsoft).

Check Python in a new terminal:

```powershell
python --version
```

### 2. Open the project

1. Copy the `jubilate-deo-chorale` folder somewhere simple, for example `C:\Projects\`.
2. In VS Code: **File > Open Folder...** and choose `jubilate-deo-chorale`.
3. Open the terminal: **Terminal > New Terminal** (`` Ctrl+` ``). It should show **PowerShell** and the project folder path.

### 3. Create a virtual environment

A virtual environment keeps this project's packages separate from the rest of your computer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

You should now see `(.venv)` at the start of the terminal line.

> **If PowerShell says "running scripts is disabled":** run this once, then activate again:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```
> Or switch the terminal to Command Prompt and use `.venv\Scripts\activate.bat`.

When VS Code asks *"We noticed a new environment..."*, click **Yes**. Otherwise press `Ctrl+Shift+P`, choose **Python: Select Interpreter**, and pick the one inside `.venv`.

### 4. Install the packages

```powershell
pip install -r requirements.txt
```

### 5. Create your private settings

```powershell
copy .env.example .env
```

Generate a secret key and paste it into `.env` as the value of `SECRET_KEY`:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

### 6. Run the app

```powershell
python app.py
```

Open <http://127.0.0.1:5000> in your browser. Stop the server with `Ctrl+C`.

**Or use the VS Code debugger:** press `F5` and pick *Jubilate Deo Chorale (Flask)*. You can set breakpoints in `app.py`.

### 7. First use

1. The first visit opens **Set up your choir**. Create the administrator account (password: at least 8 characters).
2. Sign in. You will land on the dashboard.
3. After the first account exists, the setup page is closed automatically.

### 8. Add your choir logo

Save your logo as **`static/images/logo.png`** (a square or transparent PNG works best, at least 300 x 300 px). Refresh the page. It replaces the "JDC" placeholder everywhere.

---

## Everyday commands

| Task | Command |
|---|---|
| Activate environment | `.venv\Scripts\Activate.ps1` |
| Start the app | `python app.py` |
| Install a new package | `pip install <name>` then add it to `requirements.txt` |

## Troubleshooting

- **`python` is not recognised:** reinstall Python and tick *Add python.exe to PATH*, then open a new terminal.
- **`ModuleNotFoundError: No module named 'flask'`:** the virtual environment is not active, or step 4 was skipped.
- **Port 5000 already in use:** set `PORT=5001` in `.env` and restart.
- **Session expired / "Go back and refresh" message:** refresh the page and try again. This also happens if `SECRET_KEY` changed.
- **Start over with an empty database:** stop the server, delete `instance\jubilate_deo_chorale.db`, and run again. The setup page returns.

## Notes for later phases

- Back up `instance/jubilate_deo_chorale.db` regularly; it holds all choir data.
- `.env` and the database are excluded from Git by `.gitignore`.
- Before putting this on a real server, set `FLASK_DEBUG=0` and run it behind a production server such as Waitress (`pip install waitress`).
