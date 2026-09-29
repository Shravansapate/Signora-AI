import sys
sys.path.insert(0, r'd:\Signora_AI\video2glb')
from src.bvh.bvh_reader import parse_bvh, map_bvh_to_canonical

bvh = parse_bvh(r'd:\Signora_AI\video2glb\bvh_input\A motion_20260924010030.bvh')
print(f"Total joints: {len(bvh.joints)}")
print(f"Frames: {bvh.frame_count}, FPS: {bvh.fps:.4f}")
print()
print("=== All joint names and their canonical mapping ===")
for j in bvh.joints:
    canonical = map_bvh_to_canonical(j.name)
    status = "OK" if canonical else "UNMAPPED"
    print(f"  [{status:7s}] {j.name:<40} -> {canonical or '???'}")
