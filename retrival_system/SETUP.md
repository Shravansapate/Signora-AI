# 🚀 Signora – Quick Start Setup Guide

> **Signora** is an ISL (Indian Sign Language) retrieval-based announcement system with a FastAPI + PostgreSQL backend and a Next.js frontend. It manages sign-language motion assets (GLB files), constructs signed announcements for railway stations, and delivers them to live displays through a persistent 3D avatar.

---

## 📋 Prerequisites

Install the following before starting:

| Tool | Version | Purpose | Download |
|------|---------|---------|----------|
| **Python** | 3.12 – 3.13 | Backend runtime | [python.org](https://www.python.org/downloads/) |
| **uv** | Latest | Python package manager (fast) | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| **Node.js** | ≥ 20.19.0 | Frontend runtime | [nodejs.org](https://nodejs.org/) |
| **PostgreSQL** | 18 | Database | [postgresql.org](https://www.postgresql.org/download/) |
| **pgvector** | ≥ 0.8 | Vector similarity extension | [github.com/pgvector](https://github.com/pgvector/pgvector) |
| **Git** | Latest | Version control | [git-scm.com](https://git-scm.com/) |

> **Note:** `uv` is the recommended Python package manager. If you don't have it, you can also use `pip` with the provided `requirements.txt`, but `uv` is preferred as it respects the lockfile.

---

## 📦 Project Structure

```
signora/
├── backend/                # FastAPI + PostgreSQL backend
│   ├── app/                # Application source code
│   ├── alembic/            # Database migrations
│   ├── tests/              # Backend test suite
│   ├── tools/              # Utility scripts (local-pc.ps1, etc.)
│   ├── pyproject.toml      # Python dependencies & config
│   ├── uv.lock             # Locked dependency versions
│   └── .env.example        # ← Environment variable template
├── frontend/               # Next.js frontend
│   ├── src/                # Frontend source code
│   ├── public/             # Static assets
│   ├── tests/              # Frontend test suite
│   └── package.json        # Node.js dependencies
├── metadata_json and glb/  # Supplied ISL motion assets (149 GLBs)
│   ├── metadata/           # JSON metadata per sign
│   └── glb/                # 3D animation files
├── requirements.txt        # Flat Python deps (pip fallback)
├── README.md               # Full technical reference
└── SETUP.md                # ← You are here
```

---

## ⚡ Quick Start (Step by Step)

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/Signora-AI.git
cd Signora-AI
```

### 2. Backend Setup

```powershell
cd backend
```

#### a) Install Python dependencies

**Using `uv` (recommended):**
```powershell
uv sync --locked --extra asr --extra retrieval
```

**Using `pip` (alternative):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r ../requirements.txt
```

#### b) Configure environment variables

```powershell
Copy-Item .env.example .env
```

Open `backend/.env` in your editor and fill in:
- `SIGNORA_DATABASE_URL` – your PostgreSQL connection string
- `SIGNORA_STORAGE_ROOT` – directory for storing motion assets
- `SIGNORA_PRINCIPALS` – authentication credentials (see `.env.example` for format)

#### c) Set up the database

Make sure PostgreSQL is running, then create the database and apply migrations:

```powershell
# Create the database (run in psql or pgAdmin)
# CREATE DATABASE signora;

# Apply migrations
uv run alembic upgrade head
```

#### d) (Optional) Download AI models

For speech recognition (ASR):
```powershell
uv run --extra asr signora-asr-download artifacts/asr-model
```

For semantic retrieval (E5 embeddings):
```powershell
uv run --extra retrieval signora-encoder-download artifacts/e5-model
```

#### e) Start the backend server

```powershell
uv run --extra asr --extra retrieval uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Verify it's running: open [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

---

### 3. Frontend Setup

Open a **new terminal**:

```powershell
cd frontend
```

#### a) Install Node dependencies

```powershell
npm ci
```

#### b) Development mode

```powershell
npm run dev
```

#### c) Production build

```powershell
npm run build
npm start
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000) in your browser.

---

### 4. Import Motion Assets (First Time)

After both backend and frontend are running:

1. Open the admin panel: [http://127.0.0.1:3000/admin](http://127.0.0.1:3000/admin)
2. Enter your admin bearer token
3. Go to **Imports** and create a new import job
4. In a separate terminal, start the import worker:

```powershell
cd backend
uv run signora-worker --workers 2
```

---

## 🖥️ Application URLs

| URL | Purpose | Required Credential |
|-----|---------|-------------------|
| [localhost:3000/admin](http://127.0.0.1:3000/admin) | Admin / Library management | `admin` token |
| [localhost:3000](http://127.0.0.1:3000) | Content playback / Preview | `reviewer` or `admin` token |
| [localhost:3000/announcements](http://127.0.0.1:3000/announcements) | Operator workspace | `operator` token |
| [localhost:3000/display](http://127.0.0.1:3000/display) | Passenger display | `display` ID + token |
| [localhost:8000/docs](http://127.0.0.1:8000/docs) | API documentation (Swagger) | None |
| [localhost:8000/health/ready](http://127.0.0.1:8000/health/ready) | Health check | None |

---

## 🧪 Running Tests

### Backend tests

```powershell
cd backend
uv run ruff check app tests alembic         # Lint check
uv run --extra asr --extra retrieval pytest -q   # Run test suite
```

> Set `SIGNORA_TEST_PG_BIN` to your PostgreSQL `bin` directory if it differs from `C:/Program Files/PostgreSQL/18/bin`.

### Frontend tests

```powershell
cd frontend
npm test
npm run build              # Verify production build
npm run verify:bindings    # Check GLB binding integrity
```

---

## 🔐 Generating Auth Tokens

Signora uses SHA-256 hashed bearer tokens. To generate a token:

```python
import secrets, hashlib

# Generate a random token (save this — it's the bearer token you'll use)
token = secrets.token_hex(32)
print(f"Bearer token: {token}")

# Compute its SHA-256 digest (put this in SIGNORA_PRINCIPALS)
digest = hashlib.sha256(token.encode()).hexdigest()
print(f"SHA-256 digest: {digest}")
```

Then add the digest to `SIGNORA_PRINCIPALS` in your `.env` file.

---

## 🔧 Troubleshooting

| Problem | Solution |
|---------|----------|
| `uv: command not found` | Install uv: `pip install uv` or see [installation docs](https://docs.astral.sh/uv/getting-started/installation/) |
| PostgreSQL connection refused | Make sure PostgreSQL is running and the port/credentials match your `.env` |
| `pgvector` extension error | Install pgvector for your PostgreSQL version before running migrations |
| Frontend can't reach backend | Ensure backend is on port 8000; set `SIGNORA_BACKEND_URL` if different |
| Import worker not processing | Run `uv run signora-worker --workers 2` in the backend directory |
| `Module not found` errors | Run `uv sync --locked --extra asr --extra retrieval` again |

---

## 📄 Further Reading

- **[README.md](README.md)** – Full technical reference with validation records, lifecycle details, and deployment notes.
- **[API Docs](http://127.0.0.1:8000/docs)** – Interactive Swagger documentation (when backend is running).

---

## 🤝 Contributing

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Make your changes
3. Run tests: `cd backend && uv run pytest -q` and `cd frontend && npm test`
4. Run linting: `cd backend && uv run ruff check app tests`
5. Commit and push: `git push origin feature/your-feature`
6. Open a Pull Request

---

*Built with ❤️ for accessible Indian railway announcements*
