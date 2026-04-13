# -----------------------------------------------------------------------------
# meg_d_terminal.py
#
# Author: Isaac Assegai Travers
# Date: 4/13/2026
#
# Purpose:
#   Tkinter based UDP terminal client for the MCU JSON console bridge.
#
# Notes:
#   - Uses a background receive thread so the UI stays responsive.
#   - Uses a queue to safely move received packets into the Tkinter thread.
#   - Sends one text chunk at a time using the MCU serial_tx JSON protocol.
#   - Displays local echo in a different color from remote device output.
# -----------------------------------------------------------------------------

from __future__ import annotations

import json
import queue
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any


# -----------------------------------------------------------------------------
# OutboundPacket
#
# Purpose:
#   Hold the fields for one outbound serial_tx packet.
# -----------------------------------------------------------------------------
@dataclass
class OutboundPacket:
    device: str
    data: str
    seq: int


# -----------------------------------------------------------------------------
# UdpConsoleTerminalApp
#
# Purpose:
#   Main Tkinter application for the UDP based terminal client.
# -----------------------------------------------------------------------------
class UdpConsoleTerminalApp:

    # -------------------------------------------------------------------------
    # __init__
    #
    # Purpose:
    #   Build the full UI, initialize runtime state, and start the queue pump
    #   that keeps the interface responsive.
    # -------------------------------------------------------------------------
    def __init__(self, root: tk.Tk) -> None:

        # Store the Tk root so other methods can schedule UI work safely.
        self.root = root

        # Set basic window metadata up front so the app opens in a usable size.
        self.root.title("MCU UDP Console Terminal")
        self.root.geometry("1000x700")
        self.root.minsize(800, 520)

        # Hold the active UDP socket when connected.
        self.sock: socket.socket | None = None

        # Track whether the receiver thread should keep running.
        self.running = False

        # Hold the background receiver thread object.
        self.rxThread: threading.Thread | None = None

        # Use a queue to safely move data from the socket thread into Tk.
        # Tk widgets must only be touched from the main UI thread.
        self.uiQueue: queue.Queue[tuple[str, Any]] = queue.Queue()

        # Keep a simple outbound sequence counter because the MCU expects seq.
        self.txSeq = 1

        # Track packet counts for the small status display.
        self.rxPacketCount = 0
        self.txPacketCount = 0

        # Track the most recent inbound packet time for freshness display.
        self.lastPacketTime: float | None = None

        # Remember the current remote endpoint we are talking to.
        self.connectedIp = ""
        self.connectedPort = 0

        # Build Tk variables used by widgets and status labels.
        self.ipVar = tk.StringVar(value="10.1.10.3")
        self.portVar = tk.StringVar(value="5001")
        self.deviceVar = tk.StringVar(value="SerialLIGHT")
        self.localPortVar = tk.StringVar(value="Not bound")
        self.statusVar = tk.StringVar(value="Disconnected")
        self.packetCountVar = tk.StringVar(value="RX: 0   TX: 0")
        self.errorVar = tk.StringVar(value="")

        # Build the interface widgets in one place so layout stays organized.
        self.buildUi()

        # Start a periodic queue drain so background work can update the UI.
        self.root.after(50, self.drainUiQueue)

        # Refresh the connection status text on a timer.
        self.root.after(500, self.refreshStatus)

        # Make sure window close cleans up the socket and worker thread.
        self.root.protocol("WM_DELETE_WINDOW", self.onClose)

    # -------------------------------------------------------------------------
    # buildUi
    #
    # Purpose:
    #   Construct the main layout and widgets.
    # -------------------------------------------------------------------------
    def buildUi(self) -> None:

        # Create one padded outer frame so the whole window has breathing room.
        outerFrame = ttk.Frame(self.root, padding=10)
        outerFrame.pack(fill=tk.BOTH, expand=True)

        # Build the top connection controls where the user chooses target,
        # device name, and reconnect behavior.
        connectionFrame = ttk.LabelFrame(outerFrame, text="Connection", padding=10)
        connectionFrame.pack(fill=tk.X, expand=False)

        ttk.Label(connectionFrame, text="IP Address").grid(row=0, column=0, sticky="w")
        ttk.Entry(connectionFrame, textvariable=self.ipVar, width=18).grid(
            row=1,
            column=0,
            padx=(0, 10),
            sticky="ew",
        )

        ttk.Label(connectionFrame, text="Port").grid(row=0, column=1, sticky="w")
        ttk.Entry(connectionFrame, textvariable=self.portVar, width=8).grid(
            row=1,
            column=1,
            padx=(0, 10),
            sticky="ew",
        )

        ttk.Label(connectionFrame, text="Device").grid(row=0, column=2, sticky="w")
        ttk.Entry(connectionFrame, textvariable=self.deviceVar, width=18).grid(
            row=1,
            column=2,
            padx=(0, 10),
            sticky="ew",
        )

        self.connectButton = ttk.Button(
            connectionFrame,
            text="Connect",
            command=self.connect,
        )
        self.connectButton.grid(row=1, column=3, padx=(0, 10), sticky="ew")

        ttk.Label(connectionFrame, text="Local Port").grid(row=0, column=4, sticky="w")
        ttk.Label(connectionFrame, textvariable=self.localPortVar).grid(
            row=1,
            column=4,
            padx=(0, 10),
            sticky="w",
        )

        ttk.Label(connectionFrame, text="Status").grid(row=0, column=5, sticky="w")
        ttk.Label(connectionFrame, textvariable=self.statusVar).grid(
            row=1,
            column=5,
            padx=(0, 10),
            sticky="w",
        )

        ttk.Label(connectionFrame, text="Packet Count").grid(row=0, column=6, sticky="w")
        ttk.Label(connectionFrame, textvariable=self.packetCountVar).grid(
            row=1,
            column=6,
            sticky="w",
        )

        # Let the IP and device fields stretch as the window width changes.
        connectionFrame.columnconfigure(0, weight=1)
        connectionFrame.columnconfigure(2, weight=1)

        # Build one slim status row for quick feedback and console clearing.
        statusFrame = ttk.Frame(outerFrame)
        statusFrame.pack(fill=tk.X, expand=False, pady=(8, 8))

        ttk.Label(statusFrame, text="Status / Error:").pack(side=tk.LEFT)
        ttk.Label(statusFrame, textvariable=self.errorVar).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(
            statusFrame,
            text="Clear Console",
            command=self.clearConsole,
        ).pack(side=tk.RIGHT)

        # Build the scrolling transcript area used for both local echo and
        # remote device output.
        transcriptFrame = ttk.LabelFrame(outerFrame, text="Console", padding=8)
        transcriptFrame.pack(fill=tk.BOTH, expand=True)

        self.console = ScrolledText(
            transcriptFrame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 11),
        )
        self.console.pack(fill=tk.BOTH, expand=True)

        # Configure tags so local echo, remote text, and status lines are easy
        # to distinguish visually in the transcript.
        self.console.tag_configure("local", foreground="#1f6feb")
        self.console.tag_configure("remote", foreground="#111111")
        self.console.tag_configure("status", foreground="#666666")
        self.console.tag_configure("error", foreground="#b00020")

        # Build the send area where one chunk of text is entered and sent.
        sendFrame = ttk.LabelFrame(outerFrame, text="Send", padding=10)
        sendFrame.pack(fill=tk.X, expand=False, pady=(8, 0))

        self.inputEntry = ttk.Entry(sendFrame)
        self.inputEntry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(sendFrame, text="Send", command=self.sendInput).pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        # Pressing Enter sends exactly what is in the entry box right now.
        self.inputEntry.bind("<Return>", self.sendInputEvent)

        # Put keyboard focus on the input box so the app feels ready to use.
        self.inputEntry.focus_set()

    # -------------------------------------------------------------------------
    # appendConsole
    #
    # Purpose:
    #   Append text to the transcript using the specified visual tag.
    # -------------------------------------------------------------------------
    def appendConsole(self, text: str, tag: str) -> None:

        # Temporarily enable the text widget so we can insert new content.
        self.console.configure(state=tk.NORMAL)

        # Add the text with the requested visual tag.
        self.console.insert(tk.END, text, tag)

        # Keep the newest content visible so incoming output feels live.
        self.console.see(tk.END)

        # Restore read-only behavior after insertion.
        self.console.configure(state=tk.DISABLED)

    # -------------------------------------------------------------------------
    # clearConsole
    #
    # Purpose:
    #   Clear the visible transcript while preserving the connection state.
    # -------------------------------------------------------------------------
    def clearConsole(self) -> None:

        # Temporarily enable the widget so all content can be removed.
        self.console.configure(state=tk.NORMAL)
        self.console.delete("1.0", tk.END)
        self.console.configure(state=tk.DISABLED)

    # -------------------------------------------------------------------------
    # setError
    #
    # Purpose:
    #   Update the visible status or error message area.
    # -------------------------------------------------------------------------
    def setError(self, message: str) -> None:

        # Keep the latest status or error visible near the top of the window.
        self.errorVar.set(message)

    # -------------------------------------------------------------------------
    # setPacketCounts
    #
    # Purpose:
    #   Refresh the visible RX/TX packet counters.
    # -------------------------------------------------------------------------
    def setPacketCounts(self) -> None:

        # Keep the counters together in one compact display string.
        self.packetCountVar.set(f"RX: {self.rxPacketCount}   TX: {self.txPacketCount}")

    # -------------------------------------------------------------------------
    # refreshStatus
    #
    # Purpose:
    #   Periodically refresh the status label so it reflects whether packets
    #   have been seen recently.
    # -------------------------------------------------------------------------
    def refreshStatus(self) -> None:

        # Show a simple disconnected state if no socket is open.
        if self.sock is None:
            self.statusVar.set("Disconnected")

        # Show a waiting state if connected but nothing has come back yet.
        elif self.lastPacketTime is None:
            self.statusVar.set("Connected, waiting for reply")

        else:
            # Show how old the most recent packet is for quick feedback.
            age = time.time() - self.lastPacketTime
            self.statusVar.set(f"Connected, last packet {age:.1f}s ago")

        # Keep the status fresh without blocking the UI thread.
        self.root.after(500, self.refreshStatus)

    # -------------------------------------------------------------------------
    # validateConnectionFields
    #
    # Purpose:
    #   Validate the user entered connection fields before attempting to bind
    #   a socket and send the initial packet.
    # -------------------------------------------------------------------------
    def validateConnectionFields(self) -> tuple[str, int, str] | None:

        # Read the raw field values as the user entered them.
        ip = self.ipVar.get().strip()
        portText = self.portVar.get().strip()
        device = self.deviceVar.get().strip()

        # Require a non empty IP address.
        if not ip:
            self.setError("IP address is required.")
            return None

        # Require a non empty device name because the MCU routes by it.
        if not device:
            self.setError("Device name is required.")
            return None

        # Parse the destination UDP port and validate its range.
        try:
            port = int(portText)
        except ValueError:
            self.setError("Port must be an integer.")
            return None

        if port < 1 or port > 65535:
            self.setError("Port must be between 1 and 65535.")
            return None

        return ip, port, device

    # -------------------------------------------------------------------------
    # connect
    #
    # Purpose:
    #   Close any existing socket, open a fresh UDP socket, start the receiver
    #   thread, and send one empty serial_tx packet so the MCU latches the
    #   client endpoint.
    # -------------------------------------------------------------------------
    def connect(self) -> None:

        # Validate the visible connection fields before touching socket state.
        validated = self.validateConnectionFields()
        if validated is None:
            return

        ip, port, device = validated

        # Reset transient status for this new connection attempt.
        self.setError("")
        self.lastPacketTime = None
        self.rxPacketCount = 0
        self.txPacketCount = 0
        self.setPacketCounts()

        # Close any old socket first because reconnect should start fresh.
        self.disconnectSocketOnly()

        try:
            # Create a UDP socket and bind to an ephemeral local port.
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("", 0))

            # Use a short timeout so the receiver thread can stop quickly.
            sock.settimeout(0.2)

        except OSError as exc:
            self.setError(f"Socket open/bind failed: {exc}")
            self.statusVar.set("Disconnected")
            return

        # Store the new socket and remote target information.
        self.sock = sock
        self.running = True
        self.connectedIp = ip
        self.connectedPort = port

        # Display the real local UDP port for debugging and visibility.
        localPort = sock.getsockname()[1]
        self.localPortVar.set(str(localPort))

        # Start the background receiver thread for this new socket.
        self.rxThread = threading.Thread(
            target=self.rxLoop,
            name="udpConsoleRx",
            daemon=True,
        )
        self.rxThread.start()

        # Send one empty serial_tx packet so the MCU latches this client.
        self.sendPacket(OutboundPacket(device=device, data="", seq=self.nextSeq()))

        # Add a visible status line to the transcript without clearing it.
        self.statusVar.set("Connected, waiting for reply")
        self.appendConsole(
            f"[connected to {ip}:{port} using local UDP port {localPort} for device {device}]\n",
            "status",
        )

    # -------------------------------------------------------------------------
    # disconnectSocketOnly
    #
    # Purpose:
    #   Close the current socket and stop the receiver loop without clearing
    #   the transcript.
    # -------------------------------------------------------------------------
    def disconnectSocketOnly(self) -> None:

        # Tell the receiver thread to stop looping.
        self.running = False

        # Close any open socket so blocking operations wake up quickly.
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass

        # Forget the closed socket either way.
        self.sock = None

        # Join briefly so reconnect does not leave old threads hanging around.
        if self.rxThread is not None and self.rxThread.is_alive():
            self.rxThread.join(timeout=0.5)

        # Forget the old thread reference and visible local port.
        self.rxThread = None
        self.localPortVar.set("Not bound")

    # -------------------------------------------------------------------------
    # nextSeq
    #
    # Purpose:
    #   Return the next outbound sequence number and advance the local counter.
    # -------------------------------------------------------------------------
    def nextSeq(self) -> int:

        # Return the current value, then increment for the next packet.
        seq = self.txSeq
        self.txSeq += 1
        return seq

    # -------------------------------------------------------------------------
    # sendInputEvent
    #
    # Purpose:
    #   Tk event adapter that forwards Enter key sends into sendInput.
    # -------------------------------------------------------------------------
    def sendInputEvent(self, _event: tk.Event[Any]) -> str:

        # Send exactly the text currently entered.
        self.sendInput()

        # Stop the Entry widget from doing its default Return behavior.
        return "break"

    # -------------------------------------------------------------------------
    # sendInput
    #
    # Purpose:
    #   Read the current input box text, send it as one serial_tx packet, and
    #   display local echo in the transcript.
    # -------------------------------------------------------------------------
    def sendInput(self) -> None:

        # Refuse to send if there is no active socket.
        if self.sock is None:
            self.setError("Not connected.")
            return

        # Read exactly what the user typed in the entry box.
        text = self.inputEntry.get()

        # Ignore empty sends here. Connect handles the special empty packet.
        if text == "":
            return

        # Read the currently selected device name at send time.
        device = self.deviceVar.get().strip()
        if not device:
            self.setError("Device name is required.")
            return

        # Send the packet using the MCU console protocol.
        self.sendPacket(OutboundPacket(device=device, data=text, seq=self.nextSeq()))

        # Show local echo in a separate color so typed text is easy to spot.
        self.appendConsole(text + "\n", "local")

        # Clear the input so the next chunk can be typed immediately.
        self.inputEntry.delete(0, tk.END)

    # -------------------------------------------------------------------------
    # sendPacket
    #
    # Purpose:
    #   JSON encode one outbound serial_tx packet and transmit it over UDP.
    # -------------------------------------------------------------------------
    def sendPacket(self, packet: OutboundPacket) -> None:

        # Refuse to send if there is no active socket.
        if self.sock is None:
            self.setError("Not connected.")
            return

        # Build the outbound protocol object. Use Python JSON encoding so
        # string escaping is always handled correctly.
        packetObject = {
            "type": "serial_tx",
            "device": packet.device,
            "data": packet.data,
            "seq": packet.seq,
        }

        try:
            # Encode the object compactly and send it to the MCU console port.
            payload = json.dumps(packetObject, separators=(",", ":")).encode("utf-8")
            self.sock.sendto(payload, (self.connectedIp, self.connectedPort))

        except OSError as exc:
            self.setError(f"Send failed: {exc}")
            self.uiQueue.put(("append", (f"[send failed: {exc}]\n", "error")))
            return

        # Update packet counters after a successful send.
        self.txPacketCount += 1
        self.uiQueue.put(("packetCounts", None))

    # -------------------------------------------------------------------------
    # rxLoop
    #
    # Purpose:
    #   Receive UDP packets in a background thread, parse JSON safely, filter
    #   to the selected device, and push UI updates through the queue.
    # -------------------------------------------------------------------------
    def rxLoop(self) -> None:

        # Keep a local socket reference inside the thread for clarity.
        sock = self.sock
        if sock is None:
            return

        while self.running:
            try:
                # Wait briefly for UDP data without blocking shutdown forever.
                payload, remote = sock.recvfrom(65535)

            except socket.timeout:
                # Timeout is normal. It lets the thread re check self.running.
                continue

            except OSError:
                # Socket close or OS errors should end the thread quietly.
                break

            # Record packet arrival time for the status display.
            self.lastPacketTime = time.time()

            try:
                # Decode bytes into text and parse the JSON object.
                text = payload.decode("utf-8")
                packetObject = json.loads(text)

            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.uiQueue.put(
                    (
                        "append",
                        (f"[bad packet from {remote[0]}:{remote[1]}: {exc}]\n", "error"),
                    )
                )
                continue

            # Ignore anything that is not a JSON object.
            if not isinstance(packetObject, dict):
                continue

            # Extract just the fields this client actually cares about.
            packetType = packetObject.get("type")
            packetDevice = packetObject.get("device")
            packetData = packetObject.get("data")

            # Only accept serial_rx packets for the currently selected device.
            # This keeps the transcript focused on the device the user chose.
            if packetType != "serial_rx":
                continue

            if packetDevice != self.deviceVar.get().strip():
                continue

            if not isinstance(packetData, str):
                continue

            # Count the packet and push the received text into the UI queue.
            self.rxPacketCount += 1
            self.uiQueue.put(("packetCounts", None))
            self.uiQueue.put(("append", (packetData, "remote")))

    # -------------------------------------------------------------------------
    # drainUiQueue
    #
    # Purpose:
    #   Apply queued UI updates from the background thread without blocking the
    #   Tkinter event loop.
    # -------------------------------------------------------------------------
    def drainUiQueue(self) -> None:

        # Drain everything currently waiting so bursty traffic feels smooth.
        while True:
            try:
                messageType, payload = self.uiQueue.get_nowait()
            except queue.Empty:
                break

            # Append transcript text using the supplied display tag.
            if messageType == "append":
                text, tag = payload
                self.appendConsole(text, tag)

            # Refresh packet counters when background work changes them.
            elif messageType == "packetCounts":
                self.setPacketCounts()

        # Schedule the next drain pass so the UI remains responsive.
        self.root.after(50, self.drainUiQueue)

    # -------------------------------------------------------------------------
    # onClose
    #
    # Purpose:
    #   Shut down background work and destroy the Tk window cleanly.
    # -------------------------------------------------------------------------
    def onClose(self) -> None:

        # Stop and close the socket first so the worker thread can exit.
        self.disconnectSocketOnly()

        # Destroy the Tk root after background activity has been stopped.
        self.root.destroy()


# -----------------------------------------------------------------------------
# main
#
# Purpose:
#   Create the Tk root, build the application, and enter the Tk event loop.
# -----------------------------------------------------------------------------
def main() -> None:

    # Create the Tk root window and the application object.
    root = tk.Tk()
    UdpConsoleTerminalApp(root)

    # Hand control to Tk so the UI can run until the window closes.
    root.mainloop()


# Run the application only when this file is launched directly.
if __name__ == "__main__":
    main()
