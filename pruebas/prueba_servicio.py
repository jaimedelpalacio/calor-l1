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

for _f in (sys.stdout, sys.stderr):                      # consola de Windows (cp1252): que «≥», «…» no revienten
    if hasattr(_f, "reconfigure"):
        _f.reconfigure(encoding="utf-8", errors="replace")

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
n0 = cas["n"]
for _ in range(5):                                     # 15 min entre una y otra, pero lejos de caducar: 2 al día
    t._ultima_forzada -= 1000
    t.get(forzar=True)
comprobar(cas["n"] - n0 <= 1 and len(t._forzadas) <= 2,
          f"lejos de caducar, un 401 rota el token como mucho 2 veces al día ({len(t._forzadas)} en 24 h)")
t._ultima_forzada -= 1000
t.caduca = __import__("time").time() + 60              # cerca de caducar: sí se fuerza
n0 = cas["n"]
t.get(forzar=True)
comprobar(cas["n"] == n0 + 1, "con el access cerca de caducar, el 401 sí lo renueva")
comprobar(sum(E.TIMEOUT_CAS) < E.ESPERA_APAGADO_S < 30,
          f"la espera a CAS ({sum(E.TIMEOUT_CAS)} s) cabe en la espera al apagar ({E.ESPERA_APAGADO_S} s) y esta en los 30 s de Render")
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

comprobar(s.barridos[-1]["vis"] and s.barridos[-1]["vis"][0][:2] == [s.barridos[-1]["calor"][0]["lat"], s.barridos[-1]["calor"][0]["lon"]],
          "«vis» del barrido con calor: el sitio sale con la misma lat/lon que en calor[]")

# «vis» con el motor solo: nube encima del sitio con la zona despejada, y sitios de más de 24 h fuera
import motor as MOT                                        # noqa: E402
mv = MOT.Motor(s.zona, MOT.Parametros())
t_v = base - dt.timedelta(days=2)
ant = []
for k in range(9):
    b38, b105, _ = leer_falso("x", None)
    if k == 6:
        b38[20, 20] += 15                                  # calor
    if k == 7:
        b105[16:25, 16:25] = 250.0                         # nube fría justo encima (≈ 4×4 km), el resto despejado
    r = mv.procesa(t_v + dt.timedelta(minutes=10 * k), b38, b105, REC, previos=ant)
    ant.append({"slot": SV._iso(t_v + dt.timedelta(minutes=10 * k)), "calor": r["calor"][:50]})
    if k == 7:
        vis7, nube7 = r["vis"], r["nube_pct"]
    if k == 8:
        vis8 = r["vis"]
comprobar(len(vis7) == 1 and vis7[0][2] >= 90 and nube7 < 20,
          f"nube encima del sitio con la zona casi despejada: «vis» {vis7} (zona {nube7} % de nubes)")
comprobar(len(vis8) == 1 and vis8[0][2] == 0.0, f"despejado otra vez: «vis» {vis8}")
r = mv.procesa(t_v + dt.timedelta(hours=26), b38, b105, REC, previos=ant)
comprobar(r["vis"] == [], "sitios de hace más de 24 h: fuera de «vis»")
muchos = [{"slot": SV._iso(t_v), "calor": [{"lat": 37.0 + i / 1000, "lon": -5.8} for i in range(80)]}]
comprobar(len(MOT.sitios_recientes(t_v + dt.timedelta(hours=1), [], muchos)) == MOT.VIS_TOPE, f"como mucho {MOT.VIS_TOPE} sitios")
comprobar(len(MOT.sitios_recientes(t_v, muchos[0]["calor"], [])) == MOT.MAX_PIX,
          f"del barrido actual, solo los {MOT.MAX_PIX} que salen en calor[]")

# barrido tardío con G5: no toca las rachas ni el fondo congelado de los posteriores
mt = MOT.Motor(s.zona, MOT.Parametros())
t_t = base - dt.timedelta(days=3)
for k in (0, 1, 2, 3, 4, 5, 7, 8):                      # el 6 llega tarde, después del 8
    b38, b105, _ = leer_falso("x", None)
    if k >= 7:
        b38[20, 20] += 15
    mt.procesa(t_t + dt.timedelta(minutes=10 * k), b38, b105, REC)
desde_antes = mt.desde.copy()
fondo_antes = {h.slot: h.S38.copy() for h in mt.hist}
b38, b105, _ = leer_falso("x", None)
b38[20, 20] += 15
r6 = mt.procesa(t_t + dt.timedelta(minutes=60), b38, b105, REC, tardio=True)
h6 = next(h for h in mt.hist if h.slot == t_t + dt.timedelta(minutes=60))
comprobar(np.array_equal(mt.desde, desde_antes, equal_nan=True) and all(
    np.array_equal(h.S38, fondo_antes[h.slot], equal_nan=True) for h in mt.hist if h.slot in fondo_antes)
          and abs(float(h6.S38[20, 20])) < 2 and len(r6["calor"]) >= 1 and r6["calor"][0]["racha_min"] is None,
          f"barrido tardío: ve el calor, pero no cambia las rachas ni mete el calor en el fondo (S38 guardado {float(h6.S38[20, 20]):.1f} K)")

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
descargas = {"n": 0}


def bajar_contando(*a, **k):
    descargas["n"] += 1
    return bajar_falso(*a, **k)


SV.E.bajar_trozo = bajar_contando
s2 = SV.Servicio()
s2._reconstruir()
iguales = len(s2.motor.hist) == len(s.motor.hist) and all(
    a.slot == b.slot and np.array_equal(a.S38, b.S38, equal_nan=True) and np.array_equal(a.b105, b.b105, equal_nan=True)
    for a, b in zip(sorted(s2.motor.hist, key=lambda h: h.slot), sorted(s.motor.hist, key=lambda h: h.slot)))
comprobar(s2.fondo_de == "fondo.npz" and descargas["n"] == 0 and iguales,
          f"reinicio: el fondo sale de fondo.npz, idéntico, sin bajar nada ({s2.fondo_de}, {descargas['n']} descargas)")
SV.E.bajar_trozo = bajar_falso
s2.vuelta(); s2.ultima_vuelta = dt.datetime.now(UTC)
comprobar(len(s2.barridos) == n_antes and s2.reconstruido and len(s2.motor.hist) >= 3,
          "reinicio: recupera estado.json, tiene fondo y no duplica barridos")
h2 = SV.Servicio().salud()
comprobar(h2["estado"] == "arrancando",
          f"recién reiniciado con el último barrido del proceso anterior en error: «arrancando», no «error» ({h2['estado']})")

# fondo.npz dañado: se rehace bajando la última hora
(dd / "fondo.npz").write_bytes(b"basura")
s6 = SV.Servicio()
s6._reconstruir()
comprobar(s6.reconstruido and s6.fondo_de.startswith("rehecho") and len(s6.motor.hist) >= 3,
          f"fondo.npz dañado: se rehace la última hora bajándola ({s6.fondo_de})")

# reinicio con la red caída (y sin fondo.npz): no se da por reconstruido
(dd / "fondo.npz").unlink()
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
comprobar(not h["ok"] and h["estado"] == "arrancando", f"recién reiniciado, antes de analizar nada: «{h['estado']}», nunca «ok»")
comprobar("caduca" not in h["token"] and set(h["token"]) == {"ok", "volver_a_entrar"}, "/health no enseña la caducidad del token")

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
# nubes sueltas: hay claros con fondo, pero ninguno con el 65 % de vecinos despejados (G6) → «nublado», no «sin_fondo»
s2.ultimo_ok = dict(s2.ultimo_ok, fondo_barridos=6, pix_vigilados=0, pix_con_fondo=40, vigilado=False, nube_pct=60.0)
comprobar(s2.salud()["estado"] == "nublado", f"claros con fondo pero sin vecinos despejados: «nublado» ({s2.salud()['estado']})")
inicio = s2.inicio
s2.inicio = dt.datetime.now(UTC) - dt.timedelta(hours=2)
s2.ultimo_ok = dict(s2.ultimo_ok, fondo_barridos=1, pix_vigilados=0, pix_con_fondo=0, vigilado=False, nube_pct=10.0)
comprobar(s2.salud()["estado"] == "sin_fondo", f"despejado y sin fondo de verdad: «sin_fondo» ({s2.salud()['estado']})")
s2.inicio = inicio

# G6 con G4 y G5 (motor solo): una nube fría a la izquierda del píxel (20, 20) deja despejada menos del 65 % de su
# ventana 11×11; hasta la columna 18, su dilatación tapa el píxel (lo salva G4); hasta la 17, el píxel queda despejado
def anillo(b38, b105, hasta=18):
    b105 = b105.copy()
    b105[10:31, 10:hasta + 1] = 250.0
    return b38, b105


for g4, nombre in ((True, "con calor propio (G4) se juzga aunque falten vecinos (G6 no anula a G4)"),
                   (False, "sin G4, el mismo píxel no se juzga (G6)")):
    mg = MOT.Motor(s.zona, MOT.Parametros(calor_propio=8.0 if g4 else float("inf")))
    t_g = base - dt.timedelta(days=4)
    for k in range(7):
        b38, b105, _ = leer_falso("x", None)
        if k == 6:
            b38[20, 20] += 15
            b38, b105 = anillo(b38, b105)
        r = mg.procesa(t_g + dt.timedelta(minutes=10 * k), b38, b105, REC)
    comprobar((len(r["calor"]) >= 1) == g4 and r["pix_con_fondo"] > r["pix_vigilados"], f"{nombre} ({len(r['calor'])} con calor)")

# G5 con G6: con racha abierta, despejado pero sin vecinos → su valor caliente NO entra en el fondo
mg = MOT.Motor(s.zona, MOT.Parametros(calor_propio=float("inf")))
t_g = base - dt.timedelta(days=5)
for k in range(9):
    b38, b105, _ = leer_falso("x", None)
    if k >= 6:
        b38[20, 20] += 15
    if k == 7:
        b38, b105 = anillo(b38, b105, hasta=17)
    r = mg.procesa(t_g + dt.timedelta(minutes=10 * k), b38, b105, REC)
    if k == 7:
        r7 = r
h7 = next(h for h in mg.hist if h.slot == t_g + dt.timedelta(minutes=70))
comprobar(len(r7["calor"]) == 0 and r7["pix_fondo_congelado"] >= 1 and abs(float(h7.S38[20, 20])) < 2 and len(r["calor"]) >= 1,
          f"racha abierta sin vecinos (G6): no se juzga, pero su fondo sigue congelado y luego sigue sonando "
          f"(fondo {float(h7.S38[20, 20]):.1f} K)")


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

# barridos publicados tarde, detrás de uno ya cerrado: < 30 min, se analizan sin mover el último slot; > 30 min, error
s13, s14 = s12 + dt.timedelta(minutes=10), s12 + dt.timedelta(minutes=20)
catalogo["prods"].append(prod(s14))
SV._ahora = lambda: ahora_real() + dt.timedelta(minutes=50)
s2.vuelta()                                           # s14 llega antes que s13
catalogo["prods"].append(prod(s13))
s2.vuelta()
b13 = next((b for b in s2.barridos if b["slot"].startswith(f"{s13:%Y-%m-%dT%H:%M}")), {})
comprobar(s2.ultimo_slot == s14 and b13.get("tarde") and not b13.get("error") and "pix_vigilados" in b13
          and [b["slot"] for b in s2.barridos] == sorted(b["slot"] for b in s2.barridos),
          f"publicado tarde (< 30 min): se analiza y se guarda en su sitio, sin mover el último slot ({b13.get('error')})")
s15, s16 = s14 + dt.timedelta(minutes=10), s14 + dt.timedelta(minutes=20)
catalogo["prods"].append(prod(s16))
SV._ahora = lambda: ahora_real() + dt.timedelta(minutes=70)
s2.vuelta()
catalogo["prods"].append(prod(s15))
SV._ahora = lambda: ahora_real() + dt.timedelta(minutes=105)    # s15 llega 35 min después de su fin
s2.vuelta()
comprobar(any(x.startswith(f"{s15:%Y-%m-%dT%H:%M}") for x in s2.salud()["barridos_saltados_tarde"]) and s2.ultimo_slot == s16,
          "publicado tarde (> 30 min): no se analiza, queda apuntado con error y a la vista en /health")
# un slot que nunca sale en el catálogo: pasado el plazo (fin + 30 min), «no analizado (hueco)»
s17, s18 = s16 + dt.timedelta(minutes=10), s16 + dt.timedelta(minutes=20)
catalogo["prods"].append(prod(s18))
SV._ahora = lambda: ahora_real() + dt.timedelta(minutes=135)
s2.vuelta()
b17 = next((b for b in s2.barridos if b["slot"].startswith(f"{s17:%Y-%m-%dT%H:%M}")), {})
comprobar(b17.get("hueco") and b17.get("error") == "no analizado (hueco)" and s2.ultimo_slot == s18
          and s2.salud()["huecos_24h"] >= 1, f"slot que nunca salió en el catálogo: «no analizado (hueco)» ({b17.get('error')})")
# catálogo con retraso: dos slots que no salen en 40 min quedan como hueco; si aparecen aún en orden, se analizan
s19, s20 = s18 + dt.timedelta(minutes=10), s18 + dt.timedelta(minutes=20)
SV._ahora = lambda: ahora_real() + dt.timedelta(minutes=175)
s2.vuelta()
era_hueco = any(any(b["slot"].startswith(f"{x:%Y-%m-%dT%H:%M}") for b in s2.barridos) for x in (s19, s20))
catalogo["prods"] += [prod(s19), prod(s20)]
s2.vuelta()
b19 = next((b for b in s2.barridos if b["slot"].startswith(f"{s19:%Y-%m-%dT%H:%M}")), {})
comprobar(not era_hueco and not b19.get("hueco") and "pix_vigilados" in b19 and s2.ultimo_slot == s20
          and sum(b["slot"].startswith(f"{s19:%Y-%m-%dT%H:%M}") for b in s2.barridos) == 1,
          "slots por delante del último que tardan en salir: pendientes (no hueco); al salir, se analizan en orden")

# fondo.npz de otra zona o margen: no se usa (se rehace bajando la última hora)
os.environ["MARGEN_KM"] = "2"
s7 = SV.Servicio()
s7._reconstruir()
comprobar(s7.fondo_de.startswith("rehecho"), f"fondo.npz de otro margen: no se carga ({s7.fondo_de})")
os.environ.pop("MARGEN_KM")
# fondo.npz con otro recorte (filas/columnas): el primer barrido lo descubre y rehace la hora bajándola
SV.Servicio()._reconstruir()                             # vuelve a dejar un fondo.npz de esta zona y margen
s8_ = SV.Servicio()
s8_._reconstruir()
cargado = s8_.fondo_de == "fondo.npz"
s8_._clave_fondo = (0, 1, 0, 1)
s21 = s20 + dt.timedelta(minutes=10)
catalogo["prods"].append(prod(s21))
SV._ahora = lambda: ahora_real() + dt.timedelta(minutes=185)
s8_.vuelta()
comprobar(cargado and "otro recorte" in (s8_.fondo_de or "") and s8_.ultimo_slot == s21,
          f"fondo.npz con otro recorte: se descarta y se rehace la hora antes de analizar ({s8_.fondo_de})")
SV._ahora = ahora_real

# cobertura: menos de la mitad de la zona vigilada → «parcial» (se vigila, pero no es «ok»)
s2.latido = dt.datetime.now(UTC)
s2.ultimo_cerrado = None
s2.vueltas_fallidas = 0
s2.pendientes = {}
s2.ultimo_ok = dict(s2.barridos[-1], slot=SV._iso(dt.datetime.now(UTC) - dt.timedelta(minutes=15)), fondo_barridos=6,
                    pix_vigilados=300, pix_zona=1000, cobertura_pct=30.0, nube_pct=70.0, vigilado=True)
h = s2.salud()
comprobar(h["estado"] == "parcial" and h["vigilando"] and not h["ok"] and h["cobertura_pct"] == 30.0 and h["nube_pct"] == 70.0,
          f"30 % de la zona vigilada: «parcial», vigilando, no «ok», con cobertura y nubes ({h['estado']})")
s2.ultimo_ok = dict(s2.ultimo_ok, cobertura_pct=80.0, pix_vigilados=800)
comprobar(s2.salud()["estado"] == "ok", f"80 % de la zona vigilada: «ok» ({s2.salud()['estado']})")

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

# API: /admin/token con su propia clave; sin documentación pública (se llama a las funciones: sin httpx)
os.environ["API_KEY"] = "clave-lectura"
os.environ.pop("ADMIN_KEY", None)
import importlib                                         # noqa: E402

from fastapi import HTTPException                        # noqa: E402

import app as APP                                        # noqa: E402


def codigo(f, *a, **k):
    try:
        f(*a, **k)
        return 200
    except HTTPException as ex:
        return ex.status_code


carga = APP.CargaToken(refresh_token="x")
comprobar(codigo(APP.admin_token, carga, x_api_key="clave-lectura") == 503,
          "sin ADMIN_KEY: /admin/token responde 503 «desactivado»")
os.environ["ADMIN_KEY"] = "clave-admin"
APP = importlib.reload(APP)
comprobar(codigo(APP.admin_token, carga, x_api_key="clave-lectura") == 401, "con ADMIN_KEY: la clave de lectura NO vale para /admin/token")
comprobar(codigo(APP.admin_token, APP.CargaToken(refresh_token="RT-no-vale"), x_api_key="clave-admin") == 400,
          "con ADMIN_KEY: la clave de administración sí entra (token malo: 400, el bueno sigue)")
comprobar(codigo(APP.estado, 36, x_api_key="clave-admin") == 401 and codigo(APP.estado, 36, x_api_key="clave-lectura") == 200,
          "/estado sigue con API_KEY")
rutas = {getattr(r, "path", "") for r in APP.app.routes}
comprobar(APP.app.docs_url is None and APP.app.redoc_url is None and APP.app.openapi_url is None
          and not rutas & {"/docs", "/redoc", "/openapi.json"}, f"sin /docs, /redoc ni /openapi.json ({sorted(rutas)})")
os.environ["ADMIN_KEY"] = "clave-lectura"
APP = importlib.reload(APP)
comprobar(codigo(APP.admin_token, carga, x_api_key="clave-lectura") == 503, "ADMIN_KEY igual que API_KEY: /admin/token desactivado")

print(f"\n{len(fallos)} fallos")
sys.exit(1 if fallos else 0)
