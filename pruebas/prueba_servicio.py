# prueba_servicio.py — servicio y token SIN red: se simulan EUMETSAT, el disco y los datos.
#   python pruebas/prueba_servicio.py
# Comprueba que /health no dice «ok» cuando no vigila, que un token malo no borra el bueno, que un fallo
# del disco tras renovar no pierde el token, y el arranque, los huecos y los trozos que faltan.
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

raiz = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(raiz))
os.environ["EXIGIR_DISCO"] = "0"
os.environ["BUCLE"] = "0"
import eumetsat as E          # noqa: E402
import servicio as SV         # noqa: E402
from motor import Recorte     # noqa: E402

UTC = dt.timezone.utc
fallos = []


def comprobar(cond, texto):
    print(("  OK   " if cond else "  FALLA ") + texto)
    if not cond:
        fallos.append(texto)


# ---------------------------------------------------------------- simulación de CAS (token)
class Resp:
    def __init__(self, code, data=None, texto=None):
        self.status_code, self._d, self._t = code, data, texto

    def json(self):
        if self._d is None:
            raise ValueError("no es JSON")
        return self._d


cas = {"vigente": "RT-1", "n": 1, "modo": "ok"}


def post_falso(url, data=None, timeout=None):
    if cas["modo"] == "caido":
        return Resp(503, texto="caído")
    if data["refresh_token"] != cas["vigente"]:
        return Resp(401, {"error": "invalid_grant"})
    cas["n"] += 1
    cas["vigente"] = f"RT-{cas['n']}"
    return Resp(200, {"access_token": f"AT-{cas['n']}", "refresh_token": cas["vigente"], "expires_in": 28800})


E.requests.post = post_falso

print("TOKEN")
d = Path(tempfile.mkdtemp())
(d / "tokens.json").write_text(json.dumps({"refresh_token": "RT-1"}))
t = E.Token(d / "tokens.json")
comprobar(t.get() == "AT-2" and json.loads((d / "tokens.json").read_text())["refresh_token"] == "RT-2",
          "renueva y guarda el refresh nuevo en disco")
try:
    t.cargar_refresh("RT-pegado-mal")
    comprobar(False, "un candidato malo da error")
except E.TokenError:
    comprobar(json.loads((d / "tokens.json").read_text())["refresh_token"] == "RT-2" and t.estado()["ok"],
              "un candidato malo NO borra el bueno (ni en disco ni en el estado)")
try:
    t.cargar_refresh("   ")
    comprobar(False, "un candidato vacío da error")
except E.TokenError:
    comprobar(True, "un candidato vacío se rechaza")
# disco que falla justo después de la rotación
orig = E.escribir_atomico
E.escribir_atomico = lambda *a, **k: (_ for _ in ()).throw(OSError("disco lleno"))
t.get(forzar=True)
comprobar(t._rt == cas["vigente"] and not t.estado()["ok"] and "guardar" in t.estado()["error"],
          "disco roto tras rotar: el refresh nuevo sigue en memoria y el estado lo dice")
E.escribir_atomico = orig
t.get()
comprobar(json.loads((d / "tokens.json").read_text())["refresh_token"] == cas["vigente"] and t.estado()["ok"],
          "al volver el disco se guarda en el siguiente uso")
# 401 permanente: no rota en cada intento
n0 = cas["n"]
for _ in range(5):
    t.get(forzar=True)
comprobar(cas["n"] - n0 <= 1, "forzar varias veces seguidas rota como mucho una vez cada 15 min")
# CAS caído con access aún válido: se sigue
cas["modo"] = "caido"
t.caduca = __import__("time").time() + 600            # dentro del margen: querría renovar
comprobar(t.get() is not None, "CAS caído con access vigente: se sigue con el access")
comprobar(t.estado()["error"] and not t.estado()["volver_a_entrar"], "…y el error es pasajero (no pide volver a entrar)")
cas["modo"] = "ok"

# ---------------------------------------------------------------- servicio con datos simulados
print("SERVICIO")
N = 40
lat, lon = np.meshgrid(np.linspace(37.4, 37.2, N), np.linspace(-6.0, -5.6, N), indexing="ij")
REC = Recorte(slice(0, N), slice(0, N), lat, lon, np.ones((N, N), bool), np.ones((N, N), bool), np.zeros((N, N)))
fuego = {"slot": None}


def leer_falso(ruta, zona):
    b38 = np.full((N, N), 295.0) + np.random.default_rng(1).normal(0, 0.2, (N, N))
    b105 = np.full((N, N), 290.0) + np.random.default_rng(2).normal(0, 0.2, (N, N))
    if fuego["slot"] and Path(ruta).name.startswith(fuego["slot"]):
        b38[20, 20] += 15
    return b38, b105, REC


SV.leer_trozo = leer_falso
ahora = dt.datetime.now(UTC).replace(second=0, microsecond=0)
base = ahora.replace(minute=ahora.minute // 10 * 10) - dt.timedelta(minutes=90)   # todos los barridos simulados, en el pasado
NOMBRE = "W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-HRFI-FD--CHK-BODY---NC4E_C_EUMT_{c}_IDPFI_OPE_{i}_{f}_N__O_0001_0034.nc"


def prod(s, con_trozo=True):
    f = lambda t: t.strftime("%Y%m%d%H%M%S")
    nom = NOMBRE.format(c=f(s + dt.timedelta(minutes=10)), i=f(s + dt.timedelta(minutes=8)), f=f(s + dt.timedelta(minutes=9)))
    return {"id": f"P{s:%H%M}", "slot": s, "entradas": [nom if con_trozo else nom.replace("_0034.nc", "_0033.nc")]}


catalogo = {"prods": [prod(base + dt.timedelta(minutes=10 * k)) for k in range(7)], "caido": False}
modo_bajar = {"falla": False}


def buscar_falso(col, desde, hasta):
    if catalogo["caido"]:
        raise E.requests.ConnectionError("sin red")
    return [p for p in catalogo["prods"] if desde <= p["slot"] <= hasta]


def bajar_falso(col, p, trozo, token, destino):
    if modo_bajar["falla"]:
        raise E.requests.HTTPError("503 Server Error")
    nom = next((e for e in p["entradas"] if e.endswith(f"_{trozo:04d}.nc")), None)
    if not nom:
        raise E.TrozoNoDisponible("no tiene el trozo")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"x")
    return destino, nom


BAJAR_REAL = E.bajar_trozo
SV.E.buscar, SV.E.bajar_trozo = buscar_falso, bajar_falso

dd = Path(tempfile.mkdtemp())
(dd / "tokens.json").write_text(json.dumps({"refresh_token": cas["vigente"]}))
os.environ["DATA_DIR"] = str(dd)
s = SV.Servicio()
s.vuelta(); s.ultima_vuelta = dt.datetime.now(UTC)
h = s.salud()
esperados = sum(1 for p in catalogo["prods"] if p["slot"] >= dt.datetime.now(UTC) - dt.timedelta(minutes=80))
comprobar(len(s.barridos) == esperados and h["estado"] == "ok" and h["vigilando"],
          f"arranque desde cero: la última hora y pico (6 barridos), los 3 primeros sin fondo; después vigila "
          f"(n {len(s.barridos)}, estado {h['estado']})")
comprobar([b["arranque"] for b in s.barridos][:3] == [True] * 3 and not s.barridos[3]["arranque"], "los 3 primeros marcados «arranque»")

# fuego simulado en el barrido siguiente
s8 = base + dt.timedelta(minutes=70)
catalogo["prods"].append(prod(s8))
fuego["slot"] = f"{s8:%Y%m%d%H%M}"
s.vuelta()
comprobar(s.barridos[-1]["n_calor"] >= 1 and s.barridos[-1]["calor"][0]["intenso"], "un píxel 15 K más caliente sale como calor intenso")

# descargas que fallan siempre (pasajero): tras 3 vueltas, «error» (no «ok»)
s9 = s8 + dt.timedelta(minutes=10)
catalogo["prods"].append(prod(s9))
modo_bajar["falla"] = True
for _ in range(3):
    s.vuelta(); s.ultima_vuelta = dt.datetime.now(UTC)
h = s.salud()
comprobar(h["estado"] == "error" and not h["ok"] and h["barridos_reintentando"], f"descargas fallando: estado «{h['estado']}», no «ok»")
comprobar(s.ultimo_slot == s8, "un barrido que falla se reintenta (no se abandona a la 3.ª vuelta)")
# pasados 30 min desde que se vio: se abandona y queda apuntado como error
s.pendientes[s9]["visto"] -= dt.timedelta(minutes=31)
s.vuelta()
comprobar(s.ultimo_slot == s9 and s.barridos[-1].get("error") and s.barridos[-1]["vigilado"] is False,
          "a los 30 min se abandona, apuntado como error y «vigilado: false»")
modo_bajar["falla"] = False

# barrido sin el trozo: error permanente, se cierra en el acto
s10 = s9 + dt.timedelta(minutes=10)
catalogo["prods"].append(prod(s10, con_trozo=False))
s.vuelta(); s.ultima_vuelta = dt.datetime.now(UTC)
comprobar(s.ultimo_slot == s10 and "trozo" in s.barridos[-1]["error"] and s.salud()["estado"] == "error",
          "producto sin el trozo: se cierra en el acto como error y la salud lo dice")

# bucle parado (latido)
latido = s.latido
s.latido = dt.datetime.now(UTC) - dt.timedelta(minutes=10)
comprobar(s.salud()["estado"] == "parado", "sin latido desde hace 10 min: «parado»")
s.latido = latido

# reinicio: rehace la hora anterior en silencio, sin duplicar resultados
n_antes = len(s.barridos)
s2 = SV.Servicio()
s2.vuelta(); s2.ultima_vuelta = dt.datetime.now(UTC)
comprobar(len(s2.barridos) == n_antes and s2.reconstruido and len(s2.motor.hist) >= 3,
          "reinicio: recupera estado.json, reconstruye el fondo y no duplica barridos")

# reinicio con la red caída: no se da por reconstruido
catalogo["caido"] = True
s3 = SV.Servicio()
try:
    s3.vuelta()
except Exception:
    pass
comprobar(not s3.reconstruido, "reinicio sin red: NO se marca como reconstruido (se reintentará)")
catalogo["caido"] = False

# recién reiniciado, antes de analizar nada en este proceso: «arrancando», nunca «ok»
h = SV.Servicio().salud()
comprobar(not h["ok"] and h["estado"] in ("arrancando", "error"), f"recién reiniciado, antes de analizar nada: «{h['estado']}», nunca «ok»")

# un barrido dañado con otros más nuevos esperando: se cierra al 3.er intento y se sigue con los nuevos
s11, s12 = s10 + dt.timedelta(minutes=10), s10 + dt.timedelta(minutes=20)
catalogo["prods"] += [prod(s11), prod(s12)]
orig_bajar = SV.E.bajar_trozo
SV.E.bajar_trozo = lambda col, p, *a: (_ for _ in ()).throw(ValueError("lo descargado no es un netCDF4/HDF5")) \
    if p["slot"] == s11 else orig_bajar(col, p, *a)
ahora_real = SV._ahora
SV._ahora = lambda: ahora_real() + dt.timedelta(minutes=30)      # que s11 y s12 ya estén publicados
for _ in range(3):
    s2.vuelta()
SV._ahora = ahora_real
comprobar(s2.ultimo_slot == s12 and any(b["slot"].startswith(f"{s11:%Y-%m-%dT%H:%M}") and b.get("error") for b in s2.barridos),
          "barrido dañado con otros detrás: error al 3.er intento y se analizan los siguientes (no 30 min ciego)")
SV.E.bajar_trozo = orig_bajar

# repetir la reconstrucción no duplica el fondo
m = SV.Servicio()
m._reconstruir(); m._reconstruir()
comprobar(len({h.slot for h in m.motor.hist}) == len(m.motor.hist), "reconstruir dos veces no duplica barridos en el fondo")

# cielo cubierto: «nublado», no «sin_fondo» ni «ok»
s2.latido = dt.datetime.now(UTC)
s2.ultimo_ok = dict(s2.ultimo_ok, fondo_barridos=6, pix_vigilados=0, vigilado=False, nube_pct=100.0)
comprobar(s2.salud()["estado"] == "nublado", f"todo cubierto: «nublado» ({s2.salud()['estado']})")

# CAS caído con el access aún válido: NO es «sin_token»
cas["modo"] = "caido"
s2.token.access, s2.token.caduca = "AT-vigente", __import__("time").time() + 600   # access válido, dentro del margen
s2.token.get()
h = s2.salud()
comprobar(h["estado"] != "sin_token" and h["aviso_token"], f"CAS caído con access válido: estado «{h['estado']}» y aviso, no «sin_token»")
cas["modo"] = "ok"
s2.token.error = None


# 401 que persiste tras renovar: TokenError (no un fallo del barrido)
class R401:
    status_code = 401
    def __enter__(self): return self
    def __exit__(self, *a): return False


orig_get = E.requests.get
E.requests.get = lambda *a, **k: R401()
tk = E.Token(Path(tempfile.mkdtemp()) / "tokens.json")
tk._rt, tk.access, tk.caduca = "RT-x", "AT-x", __import__("time").time() + 9999
try:
    BAJAR_REAL("C", {"id": "P", "entradas": ["x_CHK-BODY_0034.nc"]}, 34, tk, Path(tempfile.mkdtemp()) / "t.nc")
    comprobar(False, "401 persistente da TokenError")
except E.TokenError:
    comprobar(True, "401 persistente tras renovar: TokenError")
except Exception as ex:
    comprobar(False, f"401 persistente da TokenError (dio {type(ex).__name__})")
E.requests.get = orig_get

# barrido publicado tarde, detrás de uno ya cerrado: no se analiza, pero queda a la vista
s_tarde = s12 - dt.timedelta(minutes=5)
catalogo["prods"].append(prod(s_tarde))
SV._ahora = lambda: ahora_real() + dt.timedelta(minutes=30)
s2.vuelta()
SV._ahora = ahora_real
comprobar(any(x.startswith(f"{s_tarde:%Y-%m-%dT%H:%M}") for x in s2.salud()["barridos_saltados_tarde"]),
          "barrido publicado tarde (fuera de orden): se apunta en /health, no se pierde en silencio")
catalogo["prods"].remove(next(p for p in catalogo["prods"] if p["slot"] == s_tarde))

# refresh muerto con un access todavía válido: se sigue vigilando con él y /health pide volver a entrar
tk2 = E.Token(Path(tempfile.mkdtemp()) / "tokens.json")
tk2._rt, tk2.access, tk2.caduca = "RT-muerto", "AT-vale", __import__("time").time() + 600
comprobar(tk2.get() == "AT-vale" and tk2.estado()["volver_a_entrar"],
          "refresh rechazado con access aún válido: se sigue con el access y el estado pide volver a entrar")
n_cas = cas["n"]
tk2.access = None
try:
    tk2.get()
except E.TokenError:
    pass
comprobar(cas["n"] == n_cas, "token muerto: no se vuelve a llamar a CAS en cada vuelta (espera 30 min o /admin/token)")

# estado.json corrupto o con fechas sin zona
(dd / "estado.json").write_text("[1, 2, 3]")
comprobar(SV.Servicio().barridos == [], "estado.json que es una lista: arranca de cero sin romper")
(dd / "estado.json").write_text(json.dumps({"ultimo_slot": "2026-11-23T10:00:00", "barridos": [{"slot": "2026-11-23T10:00:00"}]}))
s4 = SV.Servicio()
comprobar(s4.ultimo_slot.tzinfo is not None and isinstance(s4.salud(), dict), "fechas sin zona horaria: se toman como UTC")

# sin disco persistente exigido
os.environ["EXIGIR_DISCO"] = "1"
comprobar(SV.Servicio().salud()["estado"] == "sin_disco", "con EXIGIR_DISCO=1 y sin disco montado: «sin_disco»")
os.environ["EXIGIR_DISCO"] = "0"

# sin refresh token
(dd / "tokens.json").unlink()
s5 = SV.Servicio()
comprobar(s5.salud()["estado"] == "sin_token" and s5.salud()["token"]["volver_a_entrar"], "sin refresh token: «sin_token»")

print(f"\n{len(fallos)} fallos")
sys.exit(1 if fallos else 0)
