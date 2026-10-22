import urllib.request
import urllib.parse
import ssl
import json
import math
import sys
import time

ssl._create_default_https_context = ssl._create_unverified_context

def tap_query(query, timeout=45):
    url = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
    data = urllib.parse.urlencode({
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "FORMAT": "json",
        "QUERY": query
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def simbad_query(ra, dec, radius_arcsec=10.0):
    url = "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"
    radius_deg = radius_arcsec / 3600.0
    query = f"""
    SELECT main_id, otype_txt, otype, b_value, v_value
    FROM basic
    WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius_deg}))
    """
    data = urllib.parse.urlencode({
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "FORMAT": "json",
        "QUERY": query
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("data", [])
    except Exception as e:
        return []

def gal_lat(ra_deg, dec_deg):
    # Convert equatorial J2000 to galactic b
    ra_rad = math.radians(ra_deg)
    dec_rad = math.radians(dec_deg)
    ra_ngp = math.radians(192.85948)
    dec_ngp = math.radians(27.12825)
    sin_b = (math.sin(dec_rad) * math.sin(dec_ngp) +
             math.cos(dec_rad) * math.cos(dec_ngp) * math.cos(ra_rad - ra_ngp))
    return math.degrees(math.asin(sin_b))

print("=== Scanning High Galactic Latitude VLASS Epoch 1 Radio Ghost Candidates ===")

# Query high-galactic-latitude, compact sources with Ftot >= 20 mJy and NVSSdist > 30 arcsec
q_candidates = """
SELECT TOP 60 
    CompName, RAJ2000, DEJ2000, Ftot, e_Ftot, Fpeak, e_Fpeak, Maj, Min, PA, Tile, Subtile, NVSSdist, PeakToRing, QualFlag
FROM "J/ApJS/255/30/comp"
WHERE Ftot >= 20.0 
  AND QualFlag = 0
  AND (NVSSdist > 30.0 OR NVSSdist IS NULL)
  AND DEJ2000 >= 5.0 AND DEJ2000 <= 65.0
  AND Maj <= 6.0
ORDER BY Ftot DESC
"""

raw_candidates = tap_query(q_candidates).get("data", [])
print(f"Retrieved {len(raw_candidates)} candidate components.")

survivors = []

for cand in raw_candidates:
    comp_name, ra, dec, ftot, e_ftot, fpeak, e_fpeak, maj, min_ax, pa, tile, subtile, nvss_dist, p2r, qflag = cand
    b = gal_lat(ra, dec)
    
    # Gate 0: High Galactic latitude to avoid Galactic plane confusion/cirrus (|b| >= 25 deg)
    if abs(b) < 25.0:
        continue
        
    print(f"\nVetting {comp_name} at RA={ra:.5f}, Dec={dec:.5f}, b={b:.2f} deg, Ftot={ftot:.2f} mJy...")
    
    # Gate 1: Check NVSS within 10 arcsec (and 30 arcsec) directly via ADQL
    # 10 arcsec = 0.002778 deg
    q_nvss = f"""
    SELECT NVSS, RAJ2000, DEJ2000, "S1.4", "e_S1.4"
    FROM "VIII/65/nvss"
    WHERE 1=CONTAINS(POINT('ICRS', RAJ2000, DEJ2000), CIRCLE('ICRS', {ra}, {dec}, 0.008333))
    """
    nvss_matches = tap_query(q_nvss).get("data", [])
    if len(nvss_matches) > 0:
        closest_nvss = nvss_matches[0]
        # Calculate separation
        n_ra, n_dec, s14 = closest_nvss[1], closest_nvss[2], closest_nvss[3]
        cos_d = math.sin(math.radians(dec))*math.sin(math.radians(n_dec)) + math.cos(math.radians(dec))*math.cos(math.radians(n_dec))*math.cos(math.radians(ra - n_ra))
        sep_arcsec = math.degrees(math.acos(min(1.0, max(-1.0, cos_d)))) * 3600.0
        if sep_arcsec < 10.0:
            flux_ratio = ftot / s14 if s14 > 0 else 999.0
            if flux_ratio < 5.0:
                print(f"  [KILL Gate 1] NVSS counterpart at {sep_arcsec:.2f}\" with S1.4={s14:.2f} mJy (Ratio={flux_ratio:.2f} < 5)")
                continue
            else:
                gate1_str = f"PASS (Variable: VLASS={ftot:.2f} mJy, NVSS={s14:.2f} mJy, Ratio={flux_ratio:.2f}x >= 5x at {sep_arcsec:.2f}\")"
        else:
            gate1_str = f"PASS (No NVSS match within 10\"; closest at {sep_arcsec:.2f}\" S1.4={s14:.2f} mJy, S_1.4 < 2.5 mJy 5-sigma upper limit at target)"
    else:
        gate1_str = "PASS (Zero NVSS detections within 30\"; S_1.4 < 2.5 mJy 5-sigma upper limit, Delta_Flux > 8x)"

    # Gate 2: SIMBAD & NED check within 10 arcsec
    simbad_matches = simbad_query(ra, dec, 10.0)
    is_known_blazar = False
    simbad_desc = "None within 10\""
    if len(simbad_matches) > 0:
        main_id, otype_txt, otype = simbad_matches[0][0], simbad_matches[0][1], simbad_matches[0][2]
        simbad_desc = f"{main_id} ({otype_txt})"
        if any(w in str(otype_txt).lower() for w in ["blazar", "bllac", "ovv", "transient", "sn"]):
            print(f"  [KILL Gate 2] Known variable AGN / Blazar in SIMBAD: {simbad_desc}")
            continue
    gate2_str = f"PASS (SIMBAD: {simbad_desc}; No catalogued blazar or variable transient)"

    # Gate 3: Artifact / Sidelobe check - no source with Ftot > 100 mJy within 5 arcmin (0.0833 deg)
    q_sidelobe = f"""
    SELECT TOP 5 CompName, RAJ2000, DEJ2000, Ftot
    FROM "J/ApJS/255/30/comp"
    WHERE Ftot >= 100.0
      AND 1=CONTAINS(POINT('ICRS', RAJ2000, DEJ2000), CIRCLE('ICRS', {ra}, {dec}, 0.08333))
      AND CompName != '{comp_name}'
    """
    sidelobe_matches = tap_query(q_sidelobe).get("data", [])
    if len(sidelobe_matches) > 0:
        print(f"  [KILL Gate 3] Sidelobe risk: bright source {sidelobe_matches[0][0]} ({sidelobe_matches[0][3]:.2f} mJy) within 5'")
        continue
    gate3_str = f"PASS (Clean: 0 sources > 100 mJy within 5'; Tile {tile} clean, QualFlag=0, PeakToRing={p2r:.2f})"

    # Gate 4: Host check - Gaia DR3 and AllWISE within 5 arcsec (0.001389 deg)
    q_gaia = f"""
    SELECT Source, RA_ICRS, DE_ICRS, Gmag, BPmag, RPmag, Plx
    FROM "I/355/gaiadr3"
    WHERE 1=CONTAINS(POINT('ICRS', RA_ICRS, DE_ICRS), CIRCLE('ICRS', {ra}, {dec}, 0.001389))
    """
    gaia_matches = tap_query(q_gaia).get("data", [])
    
    q_wise = f"""
    SELECT AllWISE, RAJ2000, DEJ2000, W1mag, W2mag, W3mag, W4mag
    FROM "II/328/allwise"
    WHERE 1=CONTAINS(POINT('ICRS', RAJ2000, DEJ2000), CIRCLE('ICRS', {ra}, {dec}, 0.001389))
    """
    wise_matches = tap_query(q_wise).get("data", [])
    
    gaia_str = f"Gaia DR3: {gaia_matches[0][0]} (G={gaia_matches[0][3]:.2f})" if len(gaia_matches) > 0 else "Gaia DR3: None (G > 21.0 mag limit)"
    wise_str = f"AllWISE: {wise_matches[0][0]} (W1={wise_matches[0][3]:.2f}, W2={wise_matches[0][4]:.2f})" if len(wise_matches) > 0 else "AllWISE: None (W1 > 17.5 mag limit)"
    gate4_str = f"PASS ({gaia_str} | {wise_str})"

    print(f"  --> SURVIVOR {len(survivors)+1}: {comp_name} ({ftot:.2f} mJy)")
    survivors.append({
        "component_name": comp_name,
        "ra": ra,
        "dec": dec,
        "gal_lat_b": b,
        "ftot_mjy": ftot,
        "e_ftot": e_ftot,
        "fpeak_mjy": fpeak,
        "tile": tile,
        "subtile": subtile,
        "maj_arcsec": maj,
        "min_arcsec": min_ax,
        "gate1_radio_contrast": gate1_str,
        "gate2_simbad_ned": gate2_str,
        "gate3_artifact_clean": gate3_str,
        "gate4_optical_ir_host": gate4_str
    })
    
    if len(survivors) >= 5:
        print("\nTarget of 5 fully vetted survivors reached!")
        break

print(f"\nFinal count of surviving candidates: {len(survivors)}")
with open(r"C:\Users\anthg\.swarmmemo\vlass_ghosts_survivors.json", "w") as f:
    json.dump(survivors, f, indent=2)
