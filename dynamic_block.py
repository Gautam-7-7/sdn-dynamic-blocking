# dynamic_block.py  —  POX SDN Controller  (FINAL - counting fixed)
#
# THE FIX: Count packets using a separate IP-src-only match rule
#           that sends h1's packets to controller WITHOUT installing
#           a forwarding bypass. Only after blocking do we stop counting.
#
# Mininet: sudo mn --topo single,3 --mac --controller remote,ip=127.0.0.1,port=6633
# POX:     python3.8 pox.py log.level --DEBUG dynamic_block

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.util import dpid_to_str
from pox.lib.addresses import IPAddr
from pox.lib.packet.ethernet import ethernet

log = core.getLogger()

# ── Configuration ─────────────────────────────────────────────────────────────
BLOCKED_IP       = IPAddr("10.0.0.1")
PACKET_THRESHOLD = 10
IDLE_TIMEOUT     = 10
HARD_TIMEOUT     = 30
# ─────────────────────────────────────────────────────────────────────────────

# Module-level state: persists across reconnects
_switch_state = {}  # dpid -> {'count': int, 'blocked': bool}


class DynamicBlock(object):

    def __init__(self, connection):
        self.connection  = connection
        self.dpid        = connection.dpid
        self.mac_to_port = {}

        if self.dpid not in _switch_state:
            _switch_state[self.dpid] = {'count': 0, 'blocked': False}
        self._state = _switch_state[self.dpid]

        connection.addListeners(self)
        log.info("Switch %s connected (count=%d blocked=%s)",
                 dpid_to_str(self.dpid),
                 self._state['count'],
                 self._state['blocked'])

        self._install_lldp_drop()
        self._install_ipv6_drop()
        self._install_table_miss()

        # KEY: install a "send h1 traffic to controller" rule at priority 8
        # This sits ABOVE table-miss (0) but BELOW forwarding rules (10)
        # so h1's packets always reach controller for counting,
        # even after MAC learning installs forwarding rules for other traffic.
        if not self._state['blocked']:
            self._install_h1_monitor_rule()
        else:
            # Reconnect after block — re-install the drop rule
            log.warning("Re-installing BLOCK rule after reconnect")
            self._install_block_rule()

    # ── Rule installers ───────────────────────────────────────────────────────

    def _install_lldp_drop(self):
        msg = of.ofp_flow_mod()
        msg.priority      = 6
        msg.idle_timeout  = 0
        msg.hard_timeout  = 0
        msg.match         = of.ofp_match()
        msg.match.dl_type = 0x88CC
        self.connection.send(msg)

    def _install_ipv6_drop(self):
        msg = of.ofp_flow_mod()
        msg.priority      = 5
        msg.idle_timeout  = 0
        msg.hard_timeout  = 0
        msg.match         = of.ofp_match()
        msg.match.dl_type = 0x86DD
        self.connection.send(msg)
        log.info("IPv6+LLDP drop rules installed")

    def _install_table_miss(self):
        msg = of.ofp_flow_mod()
        msg.priority     = 0
        msg.idle_timeout = 0
        msg.hard_timeout = 0
        msg.match        = of.ofp_match()
        msg.actions.append(of.ofp_action_output(port=of.OFPP_CONTROLLER))
        self.connection.send(msg)
        log.info("Table-miss rule installed")

    def _install_h1_monitor_rule(self):
        """
        THE CRITICAL FIX — priority 8 rule: match IP src=10.0.0.1,
        send to controller AND forward normally.

        Why this works:
          Without this, after the first h1 packet, _install_forward_rule()
          creates a priority-10 rule with a full 5-tuple match (src MAC,
          dst MAC, src IP, dst IP, protocol). That rule handles ALL
          subsequent matching packets IN HARDWARE — they never reach the
          controller, so the counter sticks at 1 or 2 forever.

          This rule matches ONLY on nw_src=10.0.0.1 at priority 8.
          The forwarding rule at priority 10 still wins for the exact
          5-tuple (so forwarding still works), BUT we use
          OFPP_CONTROLLER as the output — meaning the switch sends
          a copy to the controller for every h1 packet regardless.

          Actually the cleanest approach: DON'T install forwarding rules
          for h1 traffic at all while monitoring. Just send every h1
          packet to controller (priority 9, above table-miss).
          Controller forwards them manually and counts them.
          Once threshold is hit, replace with DROP rule.
        """
        msg = of.ofp_flow_mod()
        msg.priority      = 9           # above table-miss(0), below block(100)
        msg.idle_timeout  = 0           # never expire while monitoring
        msg.hard_timeout  = 0
        msg.match         = of.ofp_match()
        msg.match.dl_type = ethernet.IP_TYPE   # IPv4 only
        msg.match.nw_src  = BLOCKED_IP         # from h1 only
        # Send to controller — do NOT add a forwarding action here.
        # Controller will manually forward AND count.
        msg.actions.append(of.ofp_action_output(port=of.OFPP_CONTROLLER))
        self.connection.send(msg)
        log.info("h1 monitor rule installed (every h1 IPv4 pkt -> controller)")

    def _remove_h1_monitor_rule(self):
        """Delete the monitoring rule before installing the block rule."""
        msg = of.ofp_flow_mod()
        msg.command       = of.OFPFC_DELETE   # delete matching rules
        msg.priority      = 9
        msg.match         = of.ofp_match()
        msg.match.dl_type = ethernet.IP_TYPE
        msg.match.nw_src  = BLOCKED_IP
        self.connection.send(msg)
        log.info("h1 monitor rule removed")

    def _install_block_rule(self):
        """Priority-100 permanent DROP for h1 IPv4 traffic."""
        msg = of.ofp_flow_mod()
        msg.priority      = 100
        msg.idle_timeout  = 0
        msg.hard_timeout  = 0
        msg.match         = of.ofp_match()
        msg.match.dl_type = ethernet.IP_TYPE
        msg.match.nw_src  = BLOCKED_IP
        # No actions = DROP
        self.connection.send(msg)
        log.warning("=" * 55)
        log.warning("  *** BLOCKED %s after %d packets ***",
                    BLOCKED_IP, PACKET_THRESHOLD)
        log.warning("=" * 55)

    def _install_forward_rule(self, match, out_port):
        """Priority-10 forwarding rule for non-h1 traffic."""
        msg = of.ofp_flow_mod()
        msg.match        = match
        msg.priority     = 10
        msg.idle_timeout = IDLE_TIMEOUT
        msg.hard_timeout = HARD_TIMEOUT
        msg.actions.append(of.ofp_action_output(port=out_port))
        self.connection.send(msg)

    def _send_packet_out(self, packet_in, out_port):
        msg = of.ofp_packet_out()
        msg.in_port = packet_in.in_port
        if packet_in.buffer_id is not None and packet_in.buffer_id != of.NO_BUFFER:
            msg.buffer_id = packet_in.buffer_id
        else:
            msg.data = packet_in.data
        msg.actions.append(of.ofp_action_output(port=out_port))
        self.connection.send(msg)

    # ── Packet-in handler ─────────────────────────────────────────────────────

    def _handle_PacketIn(self, event):
        packet    = event.parsed
        packet_in = event.ofp

        if not packet.parsed:
            return

        in_port = packet_in.in_port
        src_mac = packet.src
        dst_mac = packet.dst

        # Ignore multicast sources
        src_str = str(src_mac)
        if (src_str.startswith("33:33") or
                src_str.startswith("01:00:5e") or
                src_str == "ff:ff:ff:ff:ff:ff"):
            return

        # ── MAC learning ──────────────────────────────────────────────────────
        if self.mac_to_port.get(src_mac) != in_port:
            log.info("Learned: %s -> port %d", src_mac, in_port)
            self.mac_to_port[src_mac] = in_port

        # ── Count h1 packets (arrives here via priority-9 monitor rule) ───────
        ip_pkt = packet.find('ipv4')
        if ip_pkt and ip_pkt.srcip == BLOCKED_IP and not self._state['blocked']:
            self._state['count'] += 1
            log.info("[switch %s] h1 packet %d / %d",
                     dpid_to_str(self.dpid),
                     self._state['count'],
                     PACKET_THRESHOLD)

            if self._state['count'] >= PACKET_THRESHOLD:
                self._state['blocked'] = True
                self._remove_h1_monitor_rule()   # remove monitor first
                self._install_block_rule()        # then install drop
                log.warning("h1 is now BLOCKED — dropping this packet")
                return   # drop this packet, block rule handles the rest

            # Not yet at threshold — forward this h1 packet manually
            # (no forwarding rule installed for h1, controller handles each one)
            if dst_mac in self.mac_to_port:
                self._send_packet_out(packet_in, self.mac_to_port[dst_mac])
            else:
                self._send_packet_out(packet_in, of.OFPP_FLOOD)
            return   # done — don't fall through to normal forwarding logic

        # ── Normal L2 forwarding for non-h1 traffic ───────────────────────────
        if dst_mac in self.mac_to_port:
            out_port = self.mac_to_port[dst_mac]
            match    = of.ofp_match.from_packet(packet, in_port)
            self._install_forward_rule(match, out_port)   # install HW rule
            self._send_packet_out(packet_in, out_port)
            log.debug("Fwd: %s -> %s via port %d", src_mac, dst_mac, out_port)
        else:
            log.debug("Flood: unknown dst %s", dst_mac)
            self._send_packet_out(packet_in, of.OFPP_FLOOD)


# ── POX component ─────────────────────────────────────────────────────────────

class DynamicBlockController(object):
    def __init__(self):
        core.openflow.addListeners(self)
        log.info("DynamicBlockController ready")

    def _handle_ConnectionUp(self, event):
        DynamicBlock(event.connection)

    def _handle_ConnectionDown(self, event):
        log.warning("Switch %s disconnected", dpid_to_str(event.dpid))


def launch():
    core.registerNew(DynamicBlockController)
