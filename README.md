# POX SDN Controller — Dynamic Host Blocking

A POX-based OpenFlow controller that implements MAC learning, packet-level
forwarding, and automatic blocking of a host after it exceeds a configurable
packet threshold. Built and tested with Mininet.

---

## Project Structure

```
pox/
├── pox.py                   # POX launcher (already exists in POX installation)
└── ext/
    └── dynamic_block.py     # ← place this file here
```

---

## Requirements

| Tool | Version |
|------|---------|
| Python | 3.8 |
| POX | dart / eel branch |
| Mininet | 2.3+ |
| Open vSwitch | Any recent version |
| OS | Ubuntu 20.04 / 22.04 (VM or native) |

Install Mininet if not present:
```bash
sudo apt-get install mininet
```

---

## Setup

1. Copy `dynamic_block.py` into POX's `ext/` directory:
```bash
cp dynamic_block.py ~/pox/ext/
```

2. Open **two terminals**.

---

## Running

### Terminal 1 — Start the POX Controller
```bash
cd ~/pox
python3.8 pox.py log.level --DEBUG dynamic_block
```

You should see:
```
INFO:dynamic_block:DynamicBlockController ready
```
Leave this running and watch the logs.

### Terminal 2 — Start Mininet
```bash
sudo mn --topo single,3 --mac --controller remote,ip=127.0.0.1,port=6633
```

> **`--mac` is mandatory.** It assigns static, predictable MACs to hosts:
> - h1 = `00:00:00:00:00:01` / `10.0.0.1`
> - h2 = `00:00:00:00:00:02` / `10.0.0.2`
> - h3 = `00:00:00:00:00:03` / `10.0.0.3`
>
> Without `--mac`, hosts get random MACs on every restart, breaking MAC
> learning and causing constant flooding that disconnects the controller.

---

## Testing

Run these commands in the **Mininet CLI** in order:

### Step 1 — Verify all hosts can communicate
```
mininet> pingall
```
Expected:
```
*** Results: 0% dropped (6/6 received)
```

### Step 2 — Trigger the block on h1
```
mininet> h1 ping -c 15 h2
```
Expected:
```
64 bytes from 10.0.0.2: icmp_seq=1 ...   ← packets 1-9 succeed
64 bytes from 10.0.0.2: icmp_seq=9 ...
                                           ← controller blocks h1 at packet 10
Request timeout for icmp_seq 10           ← packets 10-15 dropped
Request timeout for icmp_seq 11
...
15 packets transmitted, ~9 received, ~40% packet loss
```

### Step 3 — Confirm other hosts are unaffected
```
mininet> h2 ping -c 3 h3
```
Expected:
```
3 packets transmitted, 3 received, 0% packet loss
```

---

## Configuration

Edit these constants at the top of `dynamic_block.py`:

```python
BLOCKED_IP       = IPAddr("10.0.0.1")   # IP of the host to monitor
PACKET_THRESHOLD = 10                   # block after this many packets
IDLE_TIMEOUT     = 10                   # flow rule idle expiry (seconds)
HARD_TIMEOUT     = 30                   # flow rule hard expiry (seconds)
```

---

## How It Works

### OpenFlow Rule Priority Table

| Priority | Rule | Purpose |
|----------|------|---------|
| 100 | `nw_src=10.0.0.1, dl_type=IPv4` → DROP | Permanent block after threshold |
| 10 | Full 5-tuple match → forward | MAC-learned forwarding for h2, h3 |
| 9 | `nw_src=10.0.0.1, dl_type=IPv4` → OFPP_CONTROLLER | Monitor h1 packets (active until threshold) |
| 6 | `dl_type=0x88CC` (LLDP) → DROP | Silence LLDP frames |
| 5 | `dl_type=0x86DD` (IPv6) → DROP | Silence IPv6 ND multicast storm |
| 0 | Wildcard → OFPP_CONTROLLER | Table-miss: unknown packets to controller |

### Packet Flow (before blocking)

```
h1 sends packet
      │
      ▼
Switch checks flow table
      │
      ├─ Priority 9 matches (nw_src=10.0.0.1)
      │       │
      │       ▼
      │  Sent to Controller
      │       │
      │       ├─ Increment counter
      │       ├─ Forward packet manually (so ping still works)
      │       └─ If counter >= threshold:
      │               ├─ Delete priority-9 monitor rule
      │               └─ Install priority-100 DROP rule
      │
      └─ Other traffic → priority-10 forward rule (hardware, fast)
```

### Why h1 traffic is NOT hardware-forwarded during monitoring

For h2/h3, the controller installs a specific priority-10 flow rule
(`ofp_match.from_packet`) so subsequent packets are forwarded in hardware
without involving the controller. This is intentionally **not done** for h1.
If a forwarding rule were installed for h1, subsequent packets would bypass
the controller entirely and the counter would stall at 1 or 2 forever.
The priority-9 monitor rule ensures every h1 IPv4 packet reaches the
controller until the threshold is hit.

### Why the counter survives switch reconnections

The packet counter is stored in a module-level dict `_switch_state`, keyed
by the switch's `dpid`. When the switch reconnects and a new `DynamicBlock`
object is created, it looks up the existing state rather than starting fresh.
This is why the counter correctly continues from e.g. 6/10 after a
`connection aborted` event.

---

## Troubleshooting

### "connection aborted" in POX logs
- Cause: IPv6 Neighbor Discovery multicast packets (`33:33:xx` MACs) flood
  the OpenFlow channel, causing the keepalive echo to time out.
- Fix: Already handled by the priority-5 IPv6 drop rule. Make sure you are
  using the latest version of `dynamic_block.py`.

### pingall shows 100% dropped
- Check that the controller is running before Mininet starts.
- Check that port 6633 is not blocked: `sudo ufw allow 6633`
- Restart both controller and Mininet in order.

### h1 never gets blocked / counter stuck
- You are likely missing the `--mac` flag in the Mininet command.
- Without static MACs, forwarding rules never install correctly and the
  priority-9 monitor rule may not match consistently.

### h2/h3 traffic stops after h1 is blocked
- This should not happen. The block rule matches only `nw_src=10.0.0.1`.
- If it does, run `mininet> dpctl dump-flows` and check that no wildcard
  drop rule was installed at priority 100 without the `nw_src` field.

---

## Key Concepts Demonstrated

- **MAC learning** — controller builds a MAC-to-port table dynamically
- **Reactive forwarding** — first packet of each flow goes to controller,
  subsequent packets handled in switch hardware via installed flow rules
- **Proactive monitoring** — a standing flow rule (priority 9) ensures
  specific traffic always reaches the controller for stateful processing
- **Flow rule lifecycle** — monitor rule deleted and replaced with drop
  rule atomically at the threshold boundary
- **State persistence** — module-level state survives per-object reconnects

---

## Author

Gautam — SDN Lab, April 2026
