"""Direct test: run solver via solve_postflop_spot import and validate"""
import postflop_service as ps

result = ps.solve_postflop_spot(
    board=["Ks", "Qs", "Jd"],
    hero_pos="BB",
    vill_open_pos="BTN",
    effective_stack=100.0,
    open_size=2.2,
    rake_pct=0.0,
    rake_cap=0.0,
    iterations=5,
    equity_iters=2,
    preflop_action="CALL",
    hero_cards=["Ah", "Ad"],
    custom_oop_range={"AA": 1.0},
    custom_ip_range={"KK": 1.0},
    bet_config={
        "expand_partial_sizes": False,
        "check_raise_sizes": [3.0],
        "allow_donk": False,
        "ip_raise_sizes": [2.0],
        "oop_lead_sizes": None,
        "ip_cbet_sizes": [0.5],
        "max_raises_per_street": 1,
    },
    tree_mode="flop_to_turn",
    turn_samples=2,
    turn_card="Tc",
)

nodes = result.get("nodes") or {}

turn_nodes = [n for n in nodes.values() if n.get("street") == "TURN"]
chance_nodes = [n for n in nodes.values() if n.get("player") == "CHANCE"]

print(f"Total nodes: {len(nodes)}")
print(f"CHANCE nodes: {len(chance_nodes)}")
print(f"TURN decision nodes: {len(turn_nodes)}")

hero_action_evs_all = []
for n in turn_nodes:
    he = n.get("hero_action_evs")
    if he:
        hero_action_evs_all.append(he)
        print(f"TURN node {n['node_id']}: hero_action_evs = {he}")

bad_keys = [
    n
    for n in turn_nodes
    if n.get("player") in ("OOP", "IP", "TERMINAL")
    and not (isinstance(n.get("board_key"), list) and len(n["board_key"]) == 4)
]
if bad_keys:
    print(f"FAIL: {len(bad_keys)} TURN nodes missing a 4-card board_key")
else:
    print("PASS: All TURN nodes have 4-card board_key")

# Validate board_key
for n in turn_nodes:
    bk = n.get("board_key")
    if bk:
        print(f"  board_key = {bk}, len={len(bk)}")

if not hero_action_evs_all:
    print("FAIL: No TURN nodes had hero_action_evs")

# Check root board_key too
root_node_id = next((nid for nid in nodes if nodes[nid].get("street") == "FLOP"), None)
if root_node_id:
    print(f"Root FLOP node board_key: {nodes[root_node_id].get('board_key')}")