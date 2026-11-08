"""Only this adapter imports the experimental receive extension.

Pinned DAVE fork, hardened against plaintext fallback and transport-key rotation.
Discord.py owns MLS; we do not replace its handshake.
"""

from __future__ import annotations

import audioop
import json
import logging
import os
import queue
import time
from collections import Counter
from importlib import metadata
from typing import Protocol

COMMIT = "bec048127f4148fd147afa3182c3771b6955dc08"


class VoiceReceiveBackend(Protocol):
    async def start(self, voice_client): ...
    async def stop(self): ...


def eligible(member, own_id, channel_id, participants):
    return bool(
        member
        and member.id != own_id
        and not member.bot
        and member.id in participants
        and getattr(
            getattr(getattr(member, "voice", None), "channel", None), "id", None
        )
        == channel_id
    )


def check_dependencies():
    import discord

    try:
        metadata.version("py-cord")
    except metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError("Conflicting py-cord installation")
    if discord.__version__ != "2.7.1" or metadata.version("davey") != "0.1.6":
        raise RuntimeError("Install the pinned voice requirements")
    direct = json.loads(
        metadata.distribution("discord-ext-voice-recv").read_text("direct_url.json")
        or "{}"
    )
    if direct.get("vcs_info", {}).get("commit_id") != COMMIT:
        raise RuntimeError("Receive extension must use the audited commit")
    if metadata.version("webrtcvad-wheels") != "2.0.14":
        raise RuntimeError("Install the pinned VAD")
    if not discord.opus.is_loaded():
        if os.getenv("VOICE_OPUS_LIBRARY"):
            discord.opus.load_opus(os.environ["VOICE_OPUS_LIBRARY"])
        # discord.py can discover Opus automatically when constructing a Decoder.
        discord.opus.Decoder()


class ReceiveBackend:
    def __init__(self, participants, size=128):
        self.participants = participants
        self.frames = queue.Queue(maxsize=size)
        self.metrics = Counter()
        self.last_valid = time.monotonic()
        self.first_packet = None
        self.last_packet = 0.0
        self.failed = False
        self.active = False
        self.vc = None

    def allows(self, member):
        return self.active and eligible(
            member, self.vc.client.user.id, self.vc.channel.id, self.participants()
        )

    def accept(self, member, pcm):
        if not self.allows(member):
            return
        if len(pcm) != 3840:
            self.metrics["malformed_pcm"] += 1
            return
        self.metrics["pcm_frames"] += 1
        self.metrics["rms_peak"] = max(self.metrics["rms_peak"], audioop.rms(pcm, 2))
        self.last_valid = time.monotonic()
        try:
            self.frames.put_nowait((member.id, pcm, self.last_valid))
        except queue.Full:
            self.metrics["dropped_frames"] += 1

    def client_class(self):
        check_dependencies()
        import davey
        from discord.ext import voice_recv
        from discord.ext.voice_recv import reader, rtp

        # Upstream DEBUG paths include raw packets/transport keys. Never emit them.
        for name in list(logging.Logger.manager.loggerDict):
            if name.startswith("discord.ext.voice_recv"):
                logging.getLogger(name).disabled = True
        backend = self

        class Reader(reader.AudioReader):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.keepalive = KeepAlive(self.voice_client)

            def callback(self, data):
                backend.metrics["udp_callbacks"] += 1
                if not backend.active or self.error:
                    backend.metrics["reader_inactive_drops"] += 1
                    return
                # Ignore RTCP/discovery; they do not carry conversational audio.
                if (
                    len(data) < 12
                    or self._is_ip_discovery_packet(data)
                    or rtp.is_rtcp(data)
                ):
                    backend.metrics["non_audio_datagrams"] += 1
                    return
                failure_stage = "rtp_parse_errors"
                try:
                    packet = rtp.decode_rtp(data)
                    backend.metrics["rtp_received"] += 1
                    uid = self.voice_client._get_id_from_ssrc(packet.ssrc)
                    if uid is None:
                        backend.metrics["unknown_speaker_drops"] += 1
                        return
                    if not backend.allows(self.voice_client.guild.get_member(uid)):
                        backend.metrics["ineligible_speaker_drops"] += 1
                        return
                    backend.metrics["packets"] += 1
                    now = time.monotonic()
                    if backend.first_packet is None or now - backend.last_packet > 2:
                        backend.first_packet = now
                    backend.last_packet = now
                    connection = self.voice_client._connection
                    session = connection.dave_session
                    if (
                        not connection.dave_protocol_version
                        or not session
                        or not session.ready
                    ):
                        backend.metrics["dave_not_ready"] += 1
                        return
                    # Reconnect/epoch changes can replace session AND transport key.
                    failure_stage = "transport_decrypt_errors"
                    self.decryptor.update_secret_key(
                        bytes(self.voice_client.secret_key)
                    )
                    encrypted = self.decryptor.decrypt_rtp(packet)
                    failure_stage = "dave_decrypt_errors"
                    before = session.get_decryption_stats(uid)
                    successes = before.successes if before else 0
                    decoded = session.decrypt(uid, davey.MediaType.audio, encrypted)
                    after = session.get_decryption_stats(uid)
                    if not decoded or not after or after.successes <= successes:
                        failure_stage = "dave_unverified_drops"
                        # What is being dropped decides whether it costs words: Discord's 3-byte Opus silence
                        # frames are harmless; passed-through voice frames are lost speech. Counts only.
                        if decoded and len(decoded) <= 3:
                            backend.metrics["dave_unverified_silence"] += 1
                        elif decoded and getattr(after, "passthroughs", 0) > getattr(before, "passthroughs", 0):
                            backend.metrics["dave_unverified_passthrough_voice"] += 1
                        else:
                            backend.metrics["dave_unverified_other"] += 1
                        raise ValueError("empty_decrypted_payload")
                    packet.decrypted_data = decoded
                    backend.metrics["dave_frames"] += 1
                    failure_stage = "packet_router_errors"
                    self.speaking_timer.notify(packet.ssrc)
                    self.packet_router.feed_rtp(packet)
                except Exception:
                    backend.metrics["receive_errors"] += 1
                    # Fixed categories only. Never log packet bytes, keys, or
                    # exception text from either encryption layer.
                    backend.metrics[failure_stage] += 1

        class Client(voice_recv.VoiceRecvClient):
            reader_class = Reader

            def _remove_ssrc(self, *, user_id):
                ssrc = self._id_to_ssrc.pop(user_id, None)
                if ssrc is not None:
                    if self._reader:
                        self._reader.speaking_timer.drop_ssrc(ssrc)
                        self._reader.packet_router.destroy_decoder(ssrc)
                    self._ssrc_to_id.pop(ssrc, None)

            def listen(self, sink, *, after=None):
                if not self.is_connected() or self.is_listening():
                    raise RuntimeError("Invalid receiver state")
                self._reader = Reader(sink, self, after=after)
                self._reader.start()

        class KeepAlive(reader.UDPKeepAlive):
            def run(self):
                # Upstream sleeps 5000 seconds and cannot wake on stop. This
                # bounded daemon uses the existing packet format, not a new protocol.
                while not self._end_thread.is_set():
                    if self.voice_client.is_connected():
                        try:
                            connection = self.voice_client._connection
                            connection.socket.sendto(
                                self.counter.to_bytes(8, "big"),
                                (connection.endpoint_ip, connection.voice_port),
                            )
                            self.counter = (self.counter + 1) % (2**64)
                        except Exception:
                            pass
                    self._end_thread.wait(5)

        return Client

    def revoke(self, uid):
        if self.vc and getattr(self.vc, "_reader", None):
            ssrc = self.vc._get_ssrc_from_id(uid)
            if ssrc is not None:
                self.vc._reader.packet_router.destroy_decoder(ssrc)

    def grant(self, uid):
        if self.vc and getattr(self.vc, "_reader", None):
            ssrc = self.vc._get_ssrc_from_id(uid)
            if ssrc is not None:
                self.vc._reader.packet_router.set_user_id(ssrc, uid)

    async def start(self, voice_client):
        import discord
        from discord.ext import voice_recv

        backend = self

        class Sink(voice_recv.AudioSink):
            def wants_opus(self):
                return False

            def write(self, member, data):
                # PLC packets are not proof of successfully decrypted/decoded audio.
                if data.packet:
                    backend.accept(member, data.pcm)

            def cleanup(self):
                pass

        self.vc = voice_client
        self.active = True
        try:
            voice_client.listen(
                Sink(), after=lambda error: setattr(self, "failed", bool(error))
            )
            # Discord needs our SSRC/speaking state registered even when this
            # session only receives. Otherwise a fresh connection can receive
            # control traffic forever, until its first outbound playback.
            # State NONE sends no audio and does not light the speaking ring.
            await voice_client.ws.speak(discord.SpeakingState.none)
            self.metrics["receive_handshake_sent"] += 1
        except BaseException:
            await self.stop()
            raise

    def unhealthy(self):
        if (
            self.active
            and self.vc
            and not getattr(self.vc, "is_connected", lambda: True)()
        ):
            return True
        return self.failed or bool(
            self.first_packet
            and self.last_packet - max(self.last_valid, self.first_packet) > 10
        )

    def health(self):
        """Receiver state and counts only; never transport keys or packet data."""
        vc = self.vc
        reader = getattr(vc, "_reader", None)
        mapping = getattr(vc, "_ssrc_to_id", {})
        return {
            "reader_listening": bool(vc and vc.is_listening()),
            "reader_failed": bool(self.failed or getattr(reader, "error", None)),
            "mapped_speakers": len(mapping),
            "mapped_participants": len(set(mapping.values()) & set(self.participants())),
            "dave_ready": bool(getattr(
                getattr(getattr(vc, "_connection", None), "dave_session", None),
                "ready", False,
            )),
        }

    async def stop(self):
        self.active = False
        if self.vc and self.vc.is_listening():
            self.vc._reader.packet_router.destroy_all_decoders()
            self.vc.stop_listening()
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break
