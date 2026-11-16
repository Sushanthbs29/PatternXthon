"""ParkMind AI - FastAPI backend. SQLite by default, PostgreSQL via DATABASE_URL."""
import os, hashlib, hmac, time, math, random, jwt
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base

DB = os.getenv("DATABASE_URL", "sqlite:///./parkmind.db")
SECRET = os.getenv("SECRET_KEY", "dev-secret-change-me")
RATE = 20  # INR per hour
engine = create_engine(DB, connect_args={"check_same_thread": False} if DB.startswith("sqlite") else {})  # Singleton engine
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True); name = Column(String(80)); email = Column(String(120), unique=True)
    pw = Column(String(200)); role = Column(String(20), default="user")  # user | admin | emergency
class Area(Base):
    __tablename__ = "areas"
    id = Column(Integer, primary_key=True); name = Column(String(80))
class Slot(Base):
    __tablename__ = "slots"
    id = Column(Integer, primary_key=True); code = Column(String(12)); area_id = Column(Integer)
    status = Column(String(12), default="vacant")  # vacant | occupied | reserved
    kind = Column(String(12), default="normal")    # normal | ev | disabled | emergency
class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True); user_id = Column(Integer); slot_id = Column(Integer)
    plate = Column(String(20)); hours = Column(Integer, default=1); status = Column(String(12), default="active")
    priority = Column(Boolean, default=False); created = Column(DateTime, default=datetime.utcnow)

def hp(p, salt=None):
    salt = salt or os.urandom(8).hex()
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", p.encode(), salt.encode(), 50000).hex()
def vp(p, h): return hmac.compare_digest(hp(p, h.split("$")[0]), h)

def db():
    s = SessionLocal()
    try: yield s
    finally: s.close()

def me(c=Depends(HTTPBearer(auto_error=False)), s=Depends(db)):
    if not c: raise HTTPException(401, "Please log in")
    try: uid = int(jwt.decode(c.credentials, SECRET, algorithms=["HS256"])["sub"])
    except Exception: raise HTTPException(401, "Session expired, log in again")
    u = s.get(User, uid)
    if not u: raise HTTPException(401, "Unknown user")
    return u

def role(*roles):  # RBAC guard (Proxy pattern)
    def f(u=Depends(me)):
        if u.role not in roles: raise HTTPException(403, "Your role cannot do this")
        return u
    return f

def seed():
    Base.metadata.create_all(engine); s = SessionLocal()
    if s.query(User).count(): return s.close()
    for n, e, p, r in [("Admin", "admin@parkmind.ai", "admin123", "admin"), ("Riya Driver", "user@parkmind.ai", "user123", "user"),
                       ("Responder 1", "responder@parkmind.ai", "resp123", "emergency")]:
        s.add(User(name=n, email=e, pw=hp(p), role=r))
    s.commit(); random.seed(7)
    for name in ["Block A - Main Gate", "Block B - Mall", "Block C - Hospital"]:
        a = Area(name=name); s.add(a); s.commit(); pre = "".join(w[0] for w in name.split()[:2]).upper()
        for i in range(20):
            kind = "emergency" if i == 0 else "ev" if i % 9 == 4 else "disabled" if i == 5 else "normal"
            st = "vacant" if kind == "emergency" else random.choices(["vacant", "occupied", "reserved"], [4, 5, 1])[0]
            sl = Slot(code=f"{pre}{i+1:02d}", area_id=a.id, status=st, kind=kind); s.add(sl); s.commit()
            if st == "reserved": s.add(Booking(user_id=1, slot_id=sl.id, plate=f"KA01AB{random.randint(1000,9999)}", hours=2))
        s.commit()
    s.close()

_last = [0]
def tick(s):  # simulates sensor feed when no camera/IoT hardware is attached
    if time.time() - _last[0] < 4: return
    _last[0] = time.time()
    pool = s.query(Slot).filter(Slot.status.in_(["vacant", "occupied"]), Slot.kind != "emergency").all()
    for x in random.sample(pool, min(2, len(pool))): x.status = "vacant" if x.status == "occupied" else "occupied"
    s.commit()

def stats(s):
    sl = s.query(Slot).all(); cnt = lambda L, k: sum(1 for x in L if x.status == k)
    areas = [{"id": a.id, "name": a.name, "total": len(L), **{k: cnt(L, k) for k in ("vacant", "occupied", "reserved")}}
             for a in s.query(Area).all() for L in [[x for x in sl if x.area_id == a.id]]]
    return {"total": len(sl), **{k: cnt(sl, k) for k in ("vacant", "occupied", "reserved")}, "areas": areas}

def curve(h): return .2 + .55 * math.exp(-((h - 9) ** 2) / 6) + .65 * math.exp(-((h - 18) ** 2) / 8)
def forecast(cur):
    """Demand model: daily rush-hour curve blended with live occupancy (decays over horizon). Swap for a trained model later."""
    now = datetime.now(); hourly = []
    for i in range(13):
        h = (now.hour + i) % 24; w = math.exp(-i / 6)
        hourly.append({"hour": f"{h:02d}:00", "predicted": round(min(.98, curve(h)) * (1 - w) * 100 + cur * w * 100)})
    pk, lo = max(hourly, key=lambda x: x["predicted"]), min(hourly, key=lambda x: x["predicted"])
    weekly = [{"day": d, "occupancy": v} for d, v in zip(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], [72, 75, 78, 80, 88, 65, 48])]
    return {"hourly": hourly, "weekly": weekly, "peak": pk, "best": lo,
            "insight": f"Demand peaks around {pk['hour']} (~{pk['predicted']}% full). Easiest time to park in the next 12 hours: {lo['hour']} (~{lo['predicted']}%)."}

def slot_out(x, names): return {"id": x.id, "code": x.code, "area": names[x.area_id], "area_id": x.area_id, "status": x.status, "kind": x.kind}
def bk(b, s):
    sl = s.get(Slot, b.slot_id); return {"id": b.id, "slot": sl.code, "area": s.get(Area, sl.area_id).name, "user": s.get(User, b.user_id).name,
        "plate": b.plate, "hours": b.hours, "status": b.status, "priority": b.priority,
        "amount": 0 if b.priority else b.hours * RATE, "created": b.created.isoformat(timespec="minutes")}
def usr(u): return {"id": u.id, "name": u.name, "email": u.email, "role": u.role}
def auth_out(u): return {"token": jwt.encode({"sub": str(u.id), "exp": datetime.utcnow() + timedelta(hours=12)}, SECRET, "HS256"), "user": usr(u)}

app = FastAPI(title="ParkMind AI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
@app.on_event("startup")
def _s(): seed()

class Reg(BaseModel): name: str; email: str; password: str
class Log(BaseModel): email: str; password: str
class Bk(BaseModel): slot_id: int; plate: str; hours: int = 1
class Em(BaseModel): plate: str; reason: str = ""
class Msg(BaseModel): message: str
class NewArea(BaseModel): name: str; slots: int = 10
class RoleSet(BaseModel): role: str

@app.post("/api/auth/register")
def register(d: Reg, s=Depends(db)):
    if len(d.password) < 6: raise HTTPException(422, "Password needs 6+ characters")
    if s.query(User).filter_by(email=d.email.lower()).first(): raise HTTPException(409, "Email already registered")
    u = User(name=d.name, email=d.email.lower(), pw=hp(d.password), role="user"); s.add(u); s.commit(); return auth_out(u)
@app.post("/api/auth/login")
def login(d: Log, s=Depends(db)):
    u = s.query(User).filter_by(email=d.email.lower()).first()
    if not u or not vp(d.password, u.pw): raise HTTPException(401, "Wrong email or password")
    return auth_out(u)
@app.get("/api/me")
def whoami(u=Depends(me)): return usr(u)

@app.get("/api/slots")
def slots(u=Depends(me), s=Depends(db)):
    tick(s); names = {a.id: a.name for a in s.query(Area).all()}
    return [slot_out(x, names) for x in s.query(Slot).order_by(Slot.id).all()]
@app.get("/api/stats")
def get_stats(u=Depends(me), s=Depends(db)): tick(s); return stats(s)
@app.get("/api/predict")
def predict(u=Depends(me), s=Depends(db)):
    st = stats(s); return forecast((st["occupied"] + st["reserved"]) / max(1, st["total"]))

@app.post("/api/bookings")
def book(d: Bk, u=Depends(me), s=Depends(db)):
    sl = s.get(Slot, d.slot_id)
    if not sl or sl.status != "vacant" or sl.kind == "emergency": raise HTTPException(409, "That slot was just taken. Pick another.")
    if not 1 <= d.hours <= 24 or not d.plate.strip(): raise HTTPException(422, "Enter a plate number and 1-24 hours")
    sl.status = "reserved"; b = Booking(user_id=u.id, slot_id=sl.id, plate=d.plate.strip().upper(), hours=d.hours); s.add(b); s.commit(); return bk(b, s)
@app.get("/api/bookings")
def my_bookings(u=Depends(me), s=Depends(db)):
    return [bk(b, s) for b in s.query(Booking).filter_by(user_id=u.id).order_by(Booking.id.desc()).all()]
@app.delete("/api/bookings/{bid}")
def cancel(bid: int, u=Depends(me), s=Depends(db)):
    b = s.get(Booking, bid)
    if not b or (b.user_id != u.id and u.role != "admin"): raise HTTPException(404, "Booking not found")
    if b.status == "active": b.status = "cancelled"; s.get(Slot, b.slot_id).status = "vacant"; s.commit()
    return {"ok": True}

@app.post("/api/emergency")
def emergency(d: Em, u=Depends(role("emergency", "admin")), s=Depends(db)):
    """Strategy chain: dedicated emergency bay -> any vacant bay -> pre-empt a standard reservation."""
    sl = s.query(Slot).filter_by(status="vacant", kind="emergency").first(); note = "Dedicated emergency bay"
    if not sl: sl = s.query(Slot).filter_by(status="vacant").first(); note = "Nearest vacant bay"
    if not sl:
        old = s.query(Booking).filter_by(status="active", priority=False).first()
        if old: old.status = "preempted"; sl = s.get(Slot, old.slot_id); note = "Pre-empted a standard reservation"
    if not sl: raise HTTPException(409, "No capacity at all right now")
    sl.status = "reserved"; b = Booking(user_id=u.id, slot_id=sl.id, plate=d.plate.upper(), hours=1, priority=True); s.add(b); s.commit()
    return {**bk(b, s), "note": note}

@app.post("/api/assistant")
def assistant(d: Msg, u=Depends(me), s=Depends(db)):
    m = d.message.lower(); st = stats(s); f = forecast((st["occupied"] + st["reserved"]) / max(1, st["total"]))
    best = max(st["areas"], key=lambda a: a["vacant"]); has = lambda *k: any(x in m for x in k)
    if has("emergency", "ambulance", "urgent"): r = "Emergency vehicles get priority bays. Responder accounts can open Emergency Mode to allocate one instantly."
    elif has("book", "reserve", "my "):
        n = s.query(Booking).filter_by(user_id=u.id, status="active").count()
        r = f"You have {n} active booking(s). Open Booking to reserve a bay (₹{RATE}/hour) or cancel one."
    elif has("peak", "busy", "predict", "forecast", "when", "crowd", "time"): r = f["insight"]
    elif has("best", "recommend", "where", "nearest", "suggest"): r = f"{best['name']} has the most free bays ({best['vacant']}). Open the Parking Map to pick one."
    elif has("available", "free", "vacant", "space", "slot", "empty"):
        r = f"{st['vacant']} of {st['total']} bays are free now: " + ", ".join(f"{a['name']} {a['vacant']}" for a in st["areas"]) + "."
    elif has("price", "cost", "rate", "fee", "charge"): r = f"Parking is ₹{RATE} per hour. Emergency vehicles park free."
    else: r = "I can check free bays, recommend an area, forecast busy hours, explain pricing or summarise your bookings. Try 'where should I park?'"
    return {"reply": r}

@app.get("/api/admin/report")
def report(u=Depends(role("admin")), s=Depends(db)):
    bs = s.query(Booking).all()
    return {"users": s.query(User).count(), "bookings": len(bs), "active": sum(b.status == "active" for b in bs),
            "priority": sum(bool(b.priority) for b in bs), "revenue": sum(b.hours * RATE for b in bs if b.status != "cancelled" and not b.priority)}
@app.get("/api/admin/users")
def users(u=Depends(role("admin")), s=Depends(db)): return [usr(x) for x in s.query(User).all()]
@app.patch("/api/admin/users/{uid}")
def set_role(uid: int, d: RoleSet, u=Depends(role("admin")), s=Depends(db)):
    x = s.get(User, uid)
    if not x or d.role not in ("user", "admin", "emergency"): raise HTTPException(422, "Invalid user or role")
    x.role = d.role; s.commit(); return usr(x)
@app.get("/api/admin/bookings")
def all_bookings(u=Depends(role("admin")), s=Depends(db)): return [bk(b, s) for b in s.query(Booking).order_by(Booking.id.desc()).all()]
@app.post("/api/admin/areas")
def add_area(d: NewArea, u=Depends(role("admin")), s=Depends(db)):
    if not d.name.strip() or not 1 <= d.slots <= 100: raise HTTPException(422, "Name required, 1-100 slots")
    a = Area(name=d.name.strip()); s.add(a); s.commit(); pre = "".join(w[0] for w in d.name.split()[:2]).upper()
    s.add_all([Slot(code=f"{pre}{i+1:02d}", area_id=a.id) for i in range(d.slots)]); s.commit(); return {"id": a.id}
@app.delete("/api/admin/areas/{aid}")
def del_area(aid: int, u=Depends(role("admin")), s=Depends(db)):
    ids = [x.id for x in s.query(Slot).filter_by(area_id=aid)]
    if ids and s.query(Booking).filter(Booking.slot_id.in_(ids), Booking.status == "active").count(): raise HTTPException(409, "Area has active bookings")
    s.query(Slot).filter_by(area_id=aid).delete(); s.query(Area).filter_by(id=aid).delete(); s.commit(); return {"ok": True}
