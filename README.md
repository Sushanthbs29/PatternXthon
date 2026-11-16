# ParkMind AI
Backend:  cd backend && pip install -r requirements.txt && uvicorn main:app --reload   (docs: http://localhost:8000/docs)
Frontend: cd frontend && npm install && npm run dev   (http://localhost:5173, proxies /api to :8000)
Demo logins: admin@parkmind.ai/admin123 | user@parkmind.ai/user123 | responder@parkmind.ai/resp123
PostgreSQL: pip install psycopg2-binary; DATABASE_URL=postgresql+psycopg2://user:pw@host/parkmind; SECRET_KEY=...
UML: uml/*.puml (render with PlantUML or plantuml.com)
