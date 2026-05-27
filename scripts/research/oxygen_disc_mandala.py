"""
Oxygen 2D Disc Morphology — numerical solution to the resonant attractor state.

Theory (three pillars):
  1. Primordial substrate: nearly uniform ρ on manifold M, curved by local mass m(x).
  2. Morphology resonance: tangent collisions, generalized Snell, mass transfer Δm.
  3. Wave resonance: standing modes on the disc; collapse as geodesic descent into wells.

2D projection (oxygen, Z=8):
  ρ_2D(r, φ) = Σ_{n,l,m} |R_nl(r) Y_lm(θ=π/2, φ)|² · w_{nlm}

  Seed-of-Life interference:
  I(r, φ) = Σ_{i<j} |ψ_i + ψ_j|²  (maximized at vesica piscis crossings)

Run:
  python scripts/research/oxygen_disc_mandala.py
  python scripts/research/oxygen_disc_mandala.py --save oxygen_mandala.png
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import patches
from matplotlib.colors import LinearSegmentedColormap
from scipy.special import factorial, genlaguerre

# Bohr radius (Å) — sets radial scale for hydrogen-like orbitals
A0 = 0.529177


@dataclass(frozen=True)
class Orbital:
    n: int
    l: int
    m: int
    z_eff: float
    electrons: int
    label: str
    weight: float = 1.0


# Oxygen (Z=8): 1s² 2s² 2p⁴ — Slater Z_eff for visualization stability
ORBITALS = (
    Orbital(1, 0, 0, 7.69, 2, "1s", 1.0),
    Orbital(2, 0, 0, 4.45, 2, "2s", 0.55),
    Orbital(2, 1, -1, 4.45, 1, "2p_x", 0.85),
    Orbital(2, 1, 0, 4.45, 1, "2p_z", 0.55),  # unpaired — lower amplitude
    Orbital(2, 1, 1, 4.45, 2, "2p_y", 0.85),
)


def radial_hydrogen(r: np.ndarray, n: int, l: int, z_eff: float) -> np.ndarray:
    """Normalized hydrogen-like radial factor R_nl(r)."""
    rho = 2.0 * z_eff * r / (n * A0)
    k = n - l - 1
    lag = genlaguerre(k, 2 * l + 1)(rho)
    norm = (
        np.sqrt(
            (2.0 * z_eff / (n * A0)) ** 3 * factorial(n - l - 1)
            / (2.0 * n * factorial(n + l))
        )
        * np.exp(-rho / 2.0)
        * rho**l
    )
    return norm * lag


def spherical_harmonic(l: int, m: int, phi: np.ndarray) -> np.ndarray:
    """Y_l^m at equatorial slice θ = π/2 (colatitude), real hydrogen-like basis."""
    if l == 0:
        return np.full_like(phi, 1.0 / np.sqrt(4.0 * np.pi), dtype=np.complex128)
    if l == 1:
        # Y_1^m(θ=π/2): ∝ e^{imφ}; m=0 mode vanishes on equatorial node
        if m == 0:
            return np.zeros_like(phi, dtype=np.complex128)
        norm = np.sqrt(3.0 / (8.0 * np.pi))
        return norm * np.exp(1j * m * phi)
    raise ValueError(f"Unsupported angular momentum l={l}")


def orbital_density_superposition(
    xx: np.ndarray,
    yy: np.ndarray,
) -> np.ndarray:
    """
    Additive probability superposition on the 2D disc (gyroscope intersection):
      rho = |psi_1s|^2 + |psi_2s|^2 + |psi_2px|^2 + |psi_2py|^2 + |psi_2pz|^2
    """
    r = np.hypot(xx, yy)
    r_safe = np.maximum(r, 1e-9)
    nx = xx / r_safe
    ny = yy / r_safe

    r1s = radial_hydrogen(r, 1, 0, 7.70)
    r2s = radial_hydrogen(r, 2, 0, 4.45)
    r2p = radial_hydrogen(r, 2, 1, 4.45)

    rho = np.zeros_like(r)
    rho += 2.0 * r1s**2
    rho += 2.0 * r2s**2
    rho += 1.0 * (r2p * nx) ** 2
    rho += 2.0 * (r2p * ny) ** 2

    tilt = np.sqrt(0.5)
    pz_axis = nx * tilt + ny * tilt
    rho += 0.5 * (r2p * pz_axis) ** 2  # half-filled 2pz, projected

    dipole = 1.0 - 0.12 * ny * np.exp(-((r / (2.2 * A0)) ** 2))
    return np.maximum(rho * dipole, 0.0)


def resonance_envelope(r: np.ndarray, phi: np.ndarray, r_2p: float) -> np.ndarray:
    """
    f_resonance: harmonic lock to 8-fold (Z) and 6-fold (valence) symmetries.
    Peaks where cos(8φ) and cos(6φ) align — stable disc morphology modes.
    """
    z_fold = 0.5 * (1.0 + np.cos(8.0 * phi))
    six_fold = 0.5 * (1.0 + np.cos(6.0 * phi))
    shell = np.exp(-((r - r_2p) / (0.35 * r_2p)) ** 2)
    core = np.exp(-(r / (0.25 * r_2p)) ** 2)
    return 0.35 + 0.45 * z_fold * shell + 0.20 * six_fold * shell + 0.15 * core


def seed_of_life_centers(radius: float) -> list[tuple[float, float]]:
    """Seven-circle Seed of Life layout in the equatorial plane."""
    centers = [(0.0, 0.0)]
    for k in range(6):
        angle = k * np.pi / 3.0
        centers.append((radius * np.cos(angle), radius * np.sin(angle)))
    return centers


def vesica_interference(
    x: np.ndarray,
    y: np.ndarray,
    centers: list[tuple[float, float]],
    circle_r: float,
) -> np.ndarray:
    """
    |ψ_i + ψ_j|² proxy on disc: Gaussian modes on each circle, pairwise sum.
    Maxima appear in vesica piscis overlap regions.
    """
    modes: list[np.ndarray] = []
    sigma = circle_r * 0.55
    for cx, cy in centers:
        modes.append(np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2.0 * sigma**2)))

    interference = np.zeros_like(x)
    n = len(modes)
    for i in range(n):
        for j in range(i + 1, n):
            interference += np.abs(modes[i] + modes[j]) ** 2
    return interference / max(n * (n - 1) / 2.0, 1.0)


def fluid_boundary(radius: float, phi: np.ndarray, t_phase: float = 0.0) -> np.ndarray:
    """Wobbling outer boundary — fluid manifold edge, not a hard wall."""
    wobble = (
        0.06 * np.sin(5.0 * phi + t_phase)
        + 0.04 * np.sin(8.0 * phi - 0.7 * t_phase)
        + 0.025 * np.sin(13.0 * phi + 1.3 * t_phase)
    )
    return radius * (1.0 + wobble)


def compute_disc(
    grid: int = 512,
    r_max: float = 3.8,
) -> dict[str, np.ndarray]:
    """Evaluate ρ_2D and interference fields on a Cartesian disc slice."""
    x = np.linspace(-r_max, r_max, grid)
    y = np.linspace(-r_max, r_max, grid)
    xx, yy = np.meshgrid(x, y)
    r = np.hypot(xx, yy)
    phi = np.arctan2(yy, xx)

    r_2p = 2.05 * A0

    rho_orbitals = orbital_density_superposition(xx, yy)

    f_res = resonance_envelope(r, phi, r_2p)
    rho_2d = rho_orbitals * f_res

    circle_r = r_2p * 0.92
    centers = seed_of_life_centers(circle_r)
    interference = vesica_interference(xx, yy, centers, circle_r)

    combined = 0.72 * rho_orbitals + 0.28 * interference * np.exp(-((r / (1.1 * r_2p)) ** 2))

    boundary = fluid_boundary(r_max * 0.94, phi)
    mask = r <= boundary

    return {
        "x": xx,
        "y": yy,
        "r": r,
        "phi": phi,
        "rho_orbitals": rho_orbitals,
        "rho_2d": rho_2d,
        "interference": interference,
        "combined": combined,
        "mask": mask,
        "boundary": boundary,
        "r_2p": np.full_like(r, r_2p),
        "seed_centers": centers,
        "seed_radius": circle_r,
    }


def render(fields: dict[str, np.ndarray], save_path: Path | None = None) -> None:
    """Render the oxygen 2D disc mandala."""
    xx, yy = fields["x"], fields["y"]
    combined = fields["combined"].copy()

    fig, ax = plt.subplots(figsize=(10, 10), facecolor="#030308")
    ax.set_facecolor("#030308")

    cmap = LinearSegmentedColormap.from_list(
        "oxygen",
        ["#030308", "#0b1020", "#1a4070", "#2890c8", "#7ec8e3", "#e8f8ff", "#ffffff"],
    )

    vmax = np.nanpercentile(combined, 99.2)
    gamma = 0.62
    display = np.power(np.clip(combined / vmax, 0, 1), gamma)
    display[~fields["mask"]] = np.nan
    ax.imshow(
        display,
        origin="lower",
        extent=[xx.min(), xx.max(), yy.min(), yy.max()],
        cmap=cmap,
        vmin=0.0,
        vmax=1.0,
        interpolation="bilinear",
    )

    # Seed of Life circle outlines at 2p shell
    seed_r = fields["seed_radius"]
    for cx, cy in fields["seed_centers"]:
        circ = patches.Circle(
            (cx, cy),
            seed_r,
            fill=False,
        edgecolor=(0.72, 0.86, 1.0, 0.28),
        linewidth=0.9,
        linestyle="-",
        )
        ax.add_patch(circ)

    # 8-fold outer rose guide
    r_outer = fields["boundary"].mean() * 0.88
    for k in range(8):
        angle = k * np.pi / 4.0
        ax.plot(
            [0, r_outer * np.cos(angle)],
            [0, r_outer * np.sin(angle)],
            color=(0.55, 0.75, 0.95, 0.25),
            linewidth=0.6,
        )

    # Hexagram at 2p boundary (paired vs unpaired spin sectors)
    r_hex = seed_r * 1.05
    for tri_offset in (0.0, np.pi):
        tri = patches.RegularPolygon(
            (0, 0),
            numVertices=3,
            radius=r_hex,
            orientation=tri_offset,
            fill=False,
            edgecolor=(0.85, 0.55, 0.35, 0.45),
            linewidth=1.0,
        )
        ax.add_patch(tri)

    # Fluid boundary
    phi = fields["phi"]
    boundary = fields["boundary"]
    bx = boundary * np.cos(phi)
    by = boundary * np.sin(phi)
    ax.plot(bx, by, color=(0.68, 0.82, 1.0, 0.55), linewidth=1.4)

    # Nucleus
    r_max = float(xx.max())
    nucleus = patches.Circle((0, 0), 0.09 * r_max, facecolor=(1, 1, 1, 0.95), edgecolor="none")
    ax.add_patch(nucleus)
    ax.text(0, 0.02 * r_max, "O", ha="center", va="center", fontsize=9, color="#030308", weight="bold")
    ax.text(0, -0.07 * r_max, "Z=8", ha="center", va="center", fontsize=6, color="#c8e8ff")

    ax.set_xlim(xx.min(), xx.max())
    ax.set_ylim(yy.min(), yy.max())
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(
        "Oxygen (Z=8) — 2D Resonant Disc Morphology\n"
        r"$\rho=\sum|\psi_{nl}|^2$ superposition  ·  Seed of Life vesica  ·  Z=8 / 6 valence",
        color="#c8d8e8",
        fontsize=11,
        pad=16,
    )

    fig.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=200, bbox_inches="tight", facecolor=fig.get_facecolor())
        print(f"Saved: {save_path}")
    else:
        plt.show()


def print_parameters(fields: dict[str, np.ndarray]) -> None:
    r_2p = float(fields["r_2p"][0, 0])
    print("=== Oxygen 2D disc parameters ===")
    print(f"Atomic number Z = 8")
    print(f"2p shell scale r_2p ~ {r_2p:.3f} A")
    print(f"Seed-of-Life circle radius ~ {fields['seed_radius']:.3f} A")
    print("Orbital Slater Z_eff:")
    for orb in ORBITALS:
        print(f"  {orb.label:4s}  n={orb.n} l={orb.l} m={orb.m:+d}  Z_eff={orb.z_eff:.2f}  e-={orb.electrons}")
    print()
    print("Symmetry encoding:")
    print("  8-fold outer rose  -> Z = 8")
    print("  6-fold valence ring -> 6 bonding directions (2s2 2p4)")
    print("  2p_z weight reduced -> unpaired electron dipole (reactivity)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Oxygen 2D disc morphology simulation")
    parser.add_argument("--save", type=Path, default=None, help="Output PNG path")
    parser.add_argument("--grid", type=int, default=512, help="Grid resolution")
    parser.add_argument("--no-show", action="store_true", help="Skip interactive display")
    args = parser.parse_args()

    fields = compute_disc(grid=args.grid)
    print_parameters(fields)

    save_path = args.save
    if save_path is None and args.no_show:
        save_path = Path("oxygen_disc_mandala.png")

    if save_path or not args.no_show:
        render(fields, save_path=save_path)

    if args.no_show and save_path is None:
        render(fields, save_path=Path("oxygen_disc_mandala.png"))


if __name__ == "__main__":
    main()
