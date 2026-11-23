# -*- coding: utf-8 -*-
"""Recognise what a field holds from its name, whatever convention the resource geologist used.

Grade (Au_OK, AUppm, Cu_pct, CuEq, Li2O_%, U3O8_ppm, OK_AU, AU_ID2_CUT ...), density (SG, BD,
DENSITY_EST, Dry_Bulk_Density ...), tonnes, in-situ / volume proportion, resource category
(RESCAT, CLASS, JORC, Res_Class ...). Pure Python: used by the readers, block models and the GUI.
"""

import re

PRECIOUS = {"AU", "AG", "PT", "PD", "RH", "IR", "OS", "RU", "PGE", "PGM", "3E", "4E", "5E", "6E", "2PGE"}
PCT_DEFAULT = {"CU", "PB", "ZN", "NI", "FE", "MN", "AL", "SI", "MG", "CA", "K", "NA", "TI", "P", "S", "CR", "SN",
               "C", "GRAPHITE", "TGC", "HM", "THM", "VHM"}
OTHER = {"CO", "MO", "W", "SB", "AS", "BI", "V", "LI", "U", "TH", "NB", "TA", "ZR", "HF", "REE", "TREO", "TREE",
         "HREO", "LREO", "MREO", "GA", "GE", "CD", "TE", "SE", "HG", "BA", "SR", "CS", "RB", "Y", "SC", "LA", "CE",
         "ND", "PR", "DY", "TB", "F", "BE", "IN", "B", "CL"}
OXIDES = {"LI2O", "WO3", "U3O8", "V2O5", "FE2O3", "FEO", "AL2O3", "SIO2", "MGO", "CAO", "K2O", "NA2O", "P2O5",
          "TIO2", "MNO", "MNO2", "CR2O3", "ZRO2", "NB2O5", "TA2O5", "SNO2", "MOO3", "CEO2", "LA2O3", "ND2O3",
          "PR6O11", "DY2O3", "TB4O7", "Y2O3", "BAO", "SRO", "CS2O", "RB2O", "BEO", "GA2O3", "SC2O3", "CUO",
          "NIO", "ZNO", "PBO", "COO", "AS2O3", "SO3", "LOI", "KCL", "K2SO4", "SOP", "MOP", "CACO3", "MGCO3",
          "CAF2", "BASO4", "GRAPHITE"}
ELEMENTS = PRECIOUS | PCT_DEFAULT | OTHER | OXIDES
_SINGLE = {e for e in ELEMENTS if len(e) == 1}           # K, P, S, V, W, U, Y, F, B, C: whole token only

UNITS = {"PPM": "ppm", "PPB": "ppb", "GT": "g/t", "GPT": "g/t", "GPERT": "g/t", "G": "g/t", "PCT": "%",
         "PC": "%", "PERC": "%", "PERCENT": "%", "P": "%", "OPT": "oz/t", "OZT": "oz/t", "OZTON": "oz/t",
         "KGT": "kg/t", "LBT": "lb/t"}
# estimate / variant words that may sit next to an element symbol in a grade field name
VARIANT = {"OK", "SK", "UK", "LOK", "MIK", "ID", "ID1", "ID2", "ID3", "IDW", "IDS", "IDW2", "IDW3", "IDP", "NN",
           "PNN", "EST", "ESTIM", "FINAL", "FIN", "REP", "RPT", "CUT", "CAP", "CAPPED", "TC", "TOPCUT", "UNCUT",
           "UC", "FA", "AA", "PA", "CN", "SOL", "SCR", "TOT", "TOTAL", "AR", "ICP", "XRF", "FIRE", "EQ", "EQV",
           "EQUIV", "BEST", "PRI", "SEC", "MAIN", "GRADE", "GR", "G", "KRIG", "KRIGED", "POLY", "CK", "MEAN",
           "AVG", "COMP", "REC", "RECOV", "DIL", "DILUTED", "MINED", "INSITU", "WT", "V1", "V2", "B", "D",
           "OKD", "OKC", "CS", "RES", "RESOURCE", "SIM", "ETYPE", "RAW"}
# words that mean a field is about an estimate, not a grade (Au_NSAMP, CU_DIST, AU_PASS, AU_KV ...)
NOT_GRADE = {"DIST", "DISTANCE", "AVDIST", "NSAMP", "NSAMPLES", "NS", "NUMSAMP", "SAMP", "SAMPLES", "NHOLES",
             "NDH", "NBH", "NHOLE", "HOLES", "PASS", "SPASS", "SVOL", "VAR", "KV", "KVAR", "SLOPE", "SOR", "REGR",
             "FLAG", "CODE", "COUNT", "CNT", "NUM", "NO", "DOM", "DOMAIN", "ZONE", "SEARCH", "NCOMP", "LAGR",
             "LM", "LAGRANGE", "COV", "CV", "STD", "SD", "SDEV", "MIN", "MAX", "KE", "KEFF", "EFF", "WEIGHT",
             "OCT", "NOCT", "CLASS", "CAT", "ESTFLAG", "METHOD", "SRC", "SOURCE", "STATUS", "RANK", "ID_",
             "NNDIST", "DUP", "ERR", "ERROR", "CONF", "PROB", "ISO", "BOUND"}


def _tokens(name):
    """'Au_OK' -> ['AU', 'OK']; 'AuPPM' -> ['AU', 'PPM']; 'Cu%' -> ['CU', 'PCT']; 'Li2O_pct' -> ['LI2O', 'PCT']."""
    s = name.replace("%", "_PCT_").replace("/", "_PER_")
    s = re.sub(r"g_PER_t", "GPT", s, flags=re.I)
    s = re.sub(r"oz_PER_t(on)?", "OPT", s, flags=re.I)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", s)         # camelCase -> camel_Case (not Li2O, 3E)
    return [t for t in re.split(r"[^A-Za-z0-9]+", s.upper()) if t]


def _split_symbol(tok):
    """'AUOK' -> ('AU', 'OK'), 'AU1' -> ('AU', '1'), 'CUEQ' -> ('CU', 'EQ'), 'LI2O' -> ('LI2O', '')."""
    if tok in ELEMENTS and tok not in _SINGLE:
        return tok, ""
    for sym in sorted(ELEMENTS - _SINGLE, key=len, reverse=True):
        if tok.startswith(sym):
            rest = tok[len(sym):]
            if rest.isdigit() or rest in VARIANT or rest in UNITS:
                return sym, rest
    return None, None


def grade_info(name):
    """{'element', 'unit', 'assumed', 'kind'} if the name looks like a grade field, else None.
    kind: 'precious' (g/t -> oz), 'pct' or 'ppm' (contained metal in tonnes)."""
    toks = _tokens(name)
    if not toks or any(t in NOT_GRADE for t in toks):
        return None
    elem, unit = None, None
    rest_ok = True
    for t in toks:
        if t in UNITS and elem is not None and unit is None:
            unit = UNITS[t]
            continue
        if elem is None:
            if t in _SINGLE and (len(toks) == 1 or any(x in UNITS for x in toks)):
                elem = t
                continue
            sym, rest = _split_symbol(t)
            if sym:
                elem = sym
                if rest in UNITS:
                    unit = UNITS[rest]
                continue
        if not (t in VARIANT or t.isdigit() or t in UNITS or re.fullmatch(r"(V|R|P|S)?\d+", t)):
            rest_ok = False
    if elem is None or not rest_ok:
        return None
    if elem == "P" and len(toks) == 1:
        return None            # a lone "P" is usually not phosphorus
    assumed = unit is None
    if unit is None:
        unit = "g/t" if elem in PRECIOUS else ("%" if elem in PCT_DEFAULT or elem in OXIDES else "ppm")
    kind = "precious" if elem in PRECIOUS and unit in ("g/t", "ppm", "ppb", "oz/t") else (
        "pct" if unit == "%" else "ppm")
    return {"element": elem, "unit": unit, "assumed": assumed, "kind": kind}


_PREF = ("OK", "OKD", "FINAL", "FIN", "EST", "REP", "RPT", "BEST", "CUT", "CAP", "TC", "ID2", "ID3", "ID", "IDW",
         "NN")


def _rank(name):
    toks = _tokens(name)
    for i, p in enumerate(_PREF):
        if p in toks or any(t.endswith(p) and _split_symbol(t)[1] == p for t in toks):
            return i
    return len(_PREF) - 0.5 if len(toks) > 1 else len(_PREF) - 1  # plain 'AU' just before NN


def grade_fields(names):
    """Grade fields in a sensible display order: by first appearance of the element, then the
    estimate most likely to be the reported one (OK / FINAL / EST before ID2 before NN)."""
    g = [(n, grade_info(n)) for n in names]
    g = [(n, i) for n, i in g if i]
    order = []
    for n, i in g:
        if i["element"] not in order:
            order.append(i["element"])
    pos = {n: k for k, n in enumerate(names)}
    return [n for n, i in sorted(g, key=lambda x: (order.index(x[1]["element"]), _rank(x[0]), pos[x[0]]))]


def _core(name):
    return re.sub(r"[^A-Z0-9]", "", name.upper())


_DENS_EXACT = {"DENSITY", "DENS", "DEN", "SG", "BD", "DBD", "BULKDENSITY", "DRYDENSITY", "DRYBD", "DRYBULKDENSITY",
               "INSITUDENSITY", "RHO", "BULKD", "SGDRY", "DENSITYDRY", "DRYSG", "DENSITYT", "DENSITYGCM3", "SGEST",
               "DENSEST", "DENSITYEST", "DENSITYFINAL", "BDEST", "BDFINAL", "SGFINAL", "BULKDENS", "DENSID",
               "DENSITYOK", "DENSOK", "SGOK", "SPECIFICGRAVITY", "DENSITYASSIGNED", "DENSITYASSIGN", "INSITUSG",
               "DENSITYDBD", "DBDEST"}
_DENS_PREFIX = ("DENSITY", "DENS", "BULKDENS", "DRYBULKDENS", "DRYDENS", "INSITUDENS", "SPECIFICGRAV")
_DENS_BAD = ("FLAG", "CODE", "PASS", "METHOD", "SOURCE", "SRC", "NSAMP", "DIST", "COUNT", "CLASS", "DOM")


def density_field(names):
    """The density / SG field, or None."""
    for n in names:
        if _core(n) in _DENS_EXACT:
            return n
    for n in names:
        c = _core(n)
        if c.startswith(_DENS_PREFIX) and not any(b in c for b in _DENS_BAD):
            return n
    for n in names:                         # SG_OK, BD_ID2, SG_Assigned
        t = _tokens(n)
        if t and t[0] in ("SG", "BD", "DBD", "DENS", "DENSITY") and not any(b in _core(n) for b in _DENS_BAD):
            return n
    return None


def tonnes_field(names):
    for n in names:
        c = _core(n)
        if c in ("TONNES", "TONNAGE", "TONS", "TONNE", "TONNESDRY", "DRYTONNES", "TONNESWET", "MT", "KT",
                 "BLOCKTONNES", "BLKTONNES", "TONNESINSITU", "INSITUTONNES"):
            return n
    for n in names:
        c = _core(n)
        if c.startswith(("TONNES", "TONNAGE", "TONNE")) and not any(b in c for b in ("FACTOR", "FLAG", "CODE")):
            return n
    return None


def tonnes_scale(name):
    c = _core(name)
    return 1e6 if c == "MT" else (1e3 if c == "KT" else 1.0)


_PROP = ("INSITUPCT", "INSITU", "PCTINSITU", "INSITUPERCENT", "INSITUFRAC", "PROP", "PROPORTION", "VOLPCT",
         "VOLPROP", "VOLFRAC", "VOLFRACTION", "VOLUMEPCT", "VOLUMEFRACTION", "BLOCKPCT", "BLKPCT", "PERCENT",
         "PCT", "FRAC", "FRACTION", "PARTIAL", "PARTIALPCT", "BELOWTOPO", "BELOWTOPOPCT", "TOPOPCT", "PCTBELOWTOPO",
         "ORE", "OREPCT", "PCTORE", "MINEDPCT", "UNMINEDPCT", "PCTUNMINED", "REMAINPCT", "INSITUPROP")


def proportion_field(names):
    """A volume proportion / in-situ percentage field (scales block volume), or None."""
    for p in _PROP:
        for n in names:
            if _core(n) == p:
                return n
    return None


_CAT_EXACT = {"RESCAT", "RESCLASS", "RESCLASSIFICATION", "CLASS", "CLASSIFICATION", "CATEGORY", "RESCATEGORY",
              "CAT", "JORC", "JORCCLASS", "JORCCAT", "RESCODE", "RCAT", "MRCLASS", "CLASSCODE", "RES", "RCLASS",
              "RESOURCECLASS", "RESOURCECAT", "RESOURCECATEGORY", "RESOURCECLASSIFICATION", "CLASSIFY",
              "RESCLS", "CLS", "NI43101", "CONFIDENCE", "RESCONF", "MINRESCAT", "MRESCAT", "CLASSIFICATIONCODE",
              "RESCATFINAL", "CLASSFINAL", "CATFINAL"}


def category_field(names):
    """The resource classification field (Measured / Indicated / Inferred), or None."""
    for n in names:
        if _core(n) in _CAT_EXACT:
            return n
    for n in names:
        c = _core(n)
        if c.startswith(("RESCAT", "RESCLASS", "RESOURCECLASS", "RESOURCECAT", "JORC", "CLASSIF")):
            return n
    return None


def is_category(name):
    return category_field([name]) == name


_CAT_WORDS = {"MEAS": "Measured", "IND": "Indicated", "INF": "Inferred", "UNCL": "Unclassified",
              "EXPL": "Exploration target", "POT": "Potential", "MINERALISED": "Mineralised"}


def category_names(description):
    """{code: 'Measured', ...} from a field description like '1=Measured 2=Indicated 3=Inferred'.
    Empty when the description doesn't say (then the usual 1/2/3 = Mea/Ind/Inf is assumed)."""
    out = {}
    for code, word in re.findall(r"(-?\d+)\s*[=:\-]\s*([A-Za-z]+)", description or ""):
        w = word.upper()
        for k, v in _CAT_WORDS.items():
            if w.startswith(k):
                out[int(code)] = v
    for word, code in re.findall(r"([A-Za-z]+)\s*[=:]\s*(-?\d+)", description or ""):
        w = word.upper()
        for k, v in _CAT_WORDS.items():
            if w.startswith(k):
                out[int(code)] = v
    return out


def contained(grade_sum_t, info):
    """(value, unit label) of contained metal from sum(grade * tonnes)."""
    u, k = info["unit"], info["kind"]
    if k == "precious":
        if u == "ppb":
            return grade_sum_t / 1000.0 / 31.1035, "oz"
        if u == "oz/t":                     # troy oz per short ton
            return grade_sum_t * 1.10231, "oz"
        return grade_sum_t / 31.1035, "oz"
    if u == "%":
        return grade_sum_t / 100.0, "t"
    if u == "ppb":
        return grade_sum_t / 1e9, "t"
    if u in ("kg/t",):
        return grade_sum_t / 1000.0, "t"
    if u in ("lb/t",):
        return grade_sum_t, "lb"
    return grade_sum_t / 1e6, "t"            # ppm, g/t of a non-precious element
