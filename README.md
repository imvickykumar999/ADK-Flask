# `ADK Flask`

<img width="1536" height="1024" alt="image" src="https://github.com/user-attachments/assets/463a46d3-9438-4bfd-84d4-6000cbfe3007" />

```bash
python -m venv .venv

source .venv/bin/activate      # macOS/Linux
.\.venv\Scripts\activate       # PowerShell

pip install -r .\requirements.txt

export FLASK_APP=app.py        # macOS/Linux
set FLASK_APP=app.py           # Windows CMD
$env:FLASK_APP = "app.py"      # PowerShell

flask run
adk web
python app.py
```

    Why `flask run` failed but `python app.py` worked?

---

### 🟠 1. Different Execution Entry Points

* **`python app.py`**
  Runs **your script directly**.
  So anything under:

  ```python
  if __name__ == "__main__":
      # this part runs
      init_db()         # <- often where tables get created
      app.run(debug=True)
  ```

  **will be executed**.

* **`flask run`**
  Uses Flask’s built-in runner.
  It **does not run your script as `__main__`** — it imports the app object (`app`) only.
  So any code in the `if __name__ == "__main__": ...` block (like database initialization) **is skipped**.

👉 That’s why your database table `messages` was missing when using `flask run`.
Your `init_db()` (or `db.create_all()`) most likely lives inside that `__main__` block.

---

### 🟠 2. How to Fix It

You have two main options:

#### ✅ Option A: Move Initialization into App Factory

Move your DB setup into a function that always runs on import (or at least when the app is created):

```python
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

def create_app():
    app = Flask(__name__)
    app.config.from_object("config")

    db.init_app(app)

    with app.app_context():
        db.create_all()   # <-- ensures tables exist

    from .routes import main_bp
    app.register_blueprint(main_bp)

    return app
```

Then run:

```bash
$env:FLASK_APP = "app:create_app"
flask run
```

---

#### ✅ Option B: Call Init at Import Level

If you don’t want an app factory yet, you can do:

```python
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = "sqlite:///data.db"
db = SQLAlchemy(app)

with app.app_context():
    db.create_all()    # <-- runs even with flask run
```

This way tables are created when the module is imported.

---

### 🟠 3. Quick Diagnostic

Check your `app.py` for something like:

```python
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
```

That `init_db()` is not called by `flask run`.

---

### ✅ TL;DR

* `flask run` imports your `app` but **doesn’t run the `__main__` block**, so your DB tables weren’t created.
* `python app.py` executes the whole script, including DB setup, so it worked.
* Move your DB setup to a place that runs on import or use an **app factory**.

👉 After moving DB initialization, you can safely use `flask run` in development.
