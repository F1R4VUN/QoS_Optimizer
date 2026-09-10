#!/usr/bin/env python3
"""
qos_bandwidth_planner.py

Cisco QoS (LLQ / CBWFQ) bant genisligi yuzdelerini KEYFI vermek yerine,
gercek codec + Layer-2 overhead formulleriyle hesaplayan bir arac.

Neden var:
    "voice icin %50, video icin %30 verelim" gibi tahmini sayilar yerine,
    kac esanli cagri olacagini / hangi codec kullanilacagini / hangi L2
    tasiyiciyi kullandigini soyleyince gercek kbps ihtiyacini hesaplar,
    Cisco'nun onerdigi guardrail'leri (LLQ toplami linkin %33'unu gecmesin,
    class-default icin en az %25 pay birak) otomatik kontrol eder ve
    dogrudan router'a yapistirilabilecek bir policy-map uretir.

Kullanim:
    python3 qos_bandwidth_planner.py --help
    python3 qos_bandwidth_planner.py                      (parametre vermeden calistir -> ornek senaryo)
    python3 qos_bandwidth_planner.py --link-kbps 10000 --calls 15 --codec g711 --crtp
"""

from __future__ import annotations
from dataclasses import dataclass
import argparse
import sys


# ---------------------------------------------------------------------------
# 1) Codec tanimlari
#    payload_bytes: paket basina tasinan ses verisi, interval'e gore hesaplanir
#    interval_ms  : paketleme araligi (cogu codec 20ms kullanir)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Codec:
    name: str
    bitrate_kbps: float   # ham codec hizi (orn. G.711 = 64 kbps)
    interval_ms: int      # paketleme araligi

    @property
    def payload_bytes(self) -> float:
        return (self.bitrate_kbps * 1000 / 8) * (self.interval_ms / 1000)


CODECS = {
    "g711": Codec("G.711", 64.0, 20),
    "g729": Codec("G.729", 8.0, 20),
    "g722": Codec("G.722", 64.0, 20),
    "g723": Codec("G.723.1", 6.3, 30),
}

# Layer-2 header/overhead (byte) -- tasiyiciya gore degisir
L2_OVERHEAD = {
    "ethernet": 18,     # 14 header + 4 FCS (VLAN yok)
    "dot1q": 22,        # 802.1Q etiketli Ethernet
    "mlppp": 6,
    "ppp": 6,
    "hdlc": 4,
    "frame-relay": 4,
}

RTP_UDP_IP_HEADER = 40   # RTP(12)+UDP(8)+IP(20), sikistirilmamis
CRTP_HEADER = 4          # cRTP ile sikistirilmis (yaklasik, IOS/linke gore 2-4 byte degisebilir)

# Cisco Enterprise QoS SRND onerisi: toplam LLQ (priority) linkin %33'unu gecmemeli
LLQ_MAX_PERCENT = 33.0
# class-default / scavenger icin onerilen minimum pay
CLASS_DEFAULT_MIN_PERCENT = 25.0


def per_call_kbps(codec_key: str, l2_type: str, compressed_rtp: bool = False) -> float:
    """Tek bir cagrinin WAN uzerinde tuttugu gercek bant genisligini (kbps) hesaplar."""
    codec = CODECS[codec_key]
    l2_overhead = L2_OVERHEAD[l2_type]
    header = CRTP_HEADER if compressed_rtp else RTP_UDP_IP_HEADER

    total_packet_bytes = codec.payload_bytes + header + l2_overhead
    packets_per_second = 1000 / codec.interval_ms
    bandwidth_bps = total_packet_bytes * 8 * packets_per_second
    return bandwidth_bps / 1000


@dataclass
class QoSPlan:
    link_kbps: float
    voice_kbps: float
    video_kbps: float
    critical_kbps: float

    @property
    def voice_percent(self) -> float:
        return round(self.voice_kbps / self.link_kbps * 100, 1)

    @property
    def video_percent(self) -> float:
        return round(self.video_kbps / self.link_kbps * 100, 1)

    @property
    def critical_percent(self) -> float:
        return round(self.critical_kbps / self.link_kbps * 100, 1)

    @property
    def class_default_percent(self) -> float:
        used = self.voice_percent + self.video_percent + self.critical_percent
        return round(100 - used, 1)

    def warnings(self) -> list[str]:
        msgs = []
        if self.voice_percent > LLQ_MAX_PERCENT:
            msgs.append(
                f"VOICE (LLQ) payi %{self.voice_percent} -- Cisco'nun onerdigi "
                f"%{LLQ_MAX_PERCENT} sinirini asiyor. cRTP kullanmayi, CAC ile esanli "
                f"cagri sayisini sinirlamayi ya da linki buyutmeyi degerlendir."
            )
        if self.class_default_percent < CLASS_DEFAULT_MIN_PERCENT:
            msgs.append(
                f"class-default payi %{self.class_default_percent} -- onerilen "
                f"minimum %{CLASS_DEFAULT_MIN_PERCENT}'in altinda. Best-effort/"
                f"scavenger trafik tamamen aclikta kalabilir."
            )
        if self.class_default_percent < 0:
            msgs.append(
                "TOPLAM %100'U ASIYOR -- bu plan link kapasitesinin uzerinde talep "
                "ediyor, oldugu gibi devreye alinamaz."
            )
        return msgs

    def report(self) -> str:
        lines = [
            f"Link kapasitesi        : {self.link_kbps:.0f} kbps",
            f"VOICE (LLQ)             : {self.voice_kbps:.1f} kbps  (%{self.voice_percent})",
            f"VIDEO (CBWFQ)           : {self.video_kbps:.1f} kbps  (%{self.video_percent})",
            f"CRITICAL-DATA (CBWFQ)   : {self.critical_kbps:.1f} kbps  (%{self.critical_percent})",
            f"class-default (kalan)   : %{self.class_default_percent}",
        ]
        warn = self.warnings()
        if warn:
            lines.append("")
            lines.append("UYARILAR:")
            for w in warn:
                lines.append(f"  - {w}")
        return "\n".join(lines)

    def to_policy_map(self, name: str = "WAN-EDGE-QOS") -> str:
        """Hesaplanan yuzdelerle dogrudan router'a yapistirilabilir policy-map uretir."""
        return (
            f"policy-map {name}\n"
            f" class VOICE\n"
            f"  priority percent {round(self.voice_percent)}\n"
            f" class VIDEO\n"
            f"  bandwidth percent {round(self.video_percent)}\n"
            f"  random-detect dscp-based\n"
            f" class CRITICAL-DATA\n"
            f"  bandwidth percent {round(self.critical_percent)}\n"
            f"  random-detect dscp-based\n"
            f" class class-default\n"
            f"  fair-queue\n"
            f"  random-detect\n"
        )


def build_plan(
    link_kbps: float,
    codec_key: str,
    concurrent_calls: int,
    l2_type: str,
    video_kbps_per_session: float,
    video_sessions: int,
    critical_kbps: float,
    compressed_rtp: bool = False,
) -> QoSPlan:
    call_kbps = per_call_kbps(codec_key, l2_type, compressed_rtp)
    voice_total = call_kbps * concurrent_calls
    video_total = video_kbps_per_session * video_sessions

    return QoSPlan(
        link_kbps=link_kbps,
        voice_kbps=voice_total,
        video_kbps=video_total,
        critical_kbps=critical_kbps,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Cisco QoS (LLQ/CBWFQ) bant genisligi yuzdelerini hesaplar."
    )
    parser.add_argument("--link-kbps", type=float, required=True, help="WAN link kapasitesi (kbps)")
    parser.add_argument("--codec", choices=CODECS.keys(), default="g711")
    parser.add_argument("--calls", type=int, required=True, help="Esanli maksimum cagri sayisi")
    parser.add_argument("--l2", choices=L2_OVERHEAD.keys(), default="ethernet")
    parser.add_argument("--crtp", action="store_true", help="cRTP (sikistirilmis RTP) kullanimi")
    parser.add_argument("--video-kbps-per-session", type=float, default=0)
    parser.add_argument("--video-sessions", type=int, default=0)
    parser.add_argument("--critical-kbps", type=float, default=0)
    parser.add_argument("--policy-name", default="WAN-EDGE-QOS")

    args = parser.parse_args()

    plan = build_plan(
        link_kbps=args.link_kbps,
        codec_key=args.codec,
        concurrent_calls=args.calls,
        l2_type=args.l2,
        video_kbps_per_session=args.video_kbps_per_session,
        video_sessions=args.video_sessions,
        critical_kbps=args.critical_kbps,
        compressed_rtp=args.crtp,
    )

    print(plan.report())
    print()
    print("--- Onerilen policy-map ---")
    print(plan.to_policy_map(args.policy_name))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main()
    else:
        # Parametre verilmeden calistirilirsa ornek bir senaryo gosterir:
        # 10 Mbps WAN, G.711 ile 10 esanli cagri, 3 video oturumu, 1 Mbps kritik uygulama
        example = build_plan(
            link_kbps=10000,
            codec_key="g711",
            concurrent_calls=10,
            l2_type="ethernet",
            video_kbps_per_session=1000,
            video_sessions=3,
            critical_kbps=1000,
        )
        print("Parametre verilmedi -- ornek senaryo calistiriliyor:")
        print()
        print(example.report())
        print()
        print("--- Onerilen policy-map ---")
        print(example.to_policy_map())
