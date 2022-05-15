"""This module communicates with SigmaStudio.

It can receive read/write requests and return with read response packets.
"""
import logging
import socket
import socketserver
from typing import Union

from sigmadsp.communication.base import (
    ReadRequest,
    SafeloadRequest,
    ThreadedTCPServer,
    WriteRequest,
)
from sigmadsp.helper.conversion import (
    bytes_to_int8,
    bytes_to_int16,
    bytes_to_int32,
    int8_to_bytes,
    int16_to_bytes,
    int32_to_bytes,
)

# A logger for this module
logger = logging.getLogger(__name__)


class SigmaStudioRequestHandler(socketserver.BaseRequestHandler):
    """Request handler for messages from SigmaStudio."""

    request: socket.socket
    server: ThreadedTCPServer

    HEADER_LENGTH: int = 14
    COMMAND_WRITE: int = 0x09
    COMMAND_READ: int = 0x0A
    COMMAND_READ_RESPONSE: int = 0x0B

    def receive_amount(self, total_size: int) -> bytearray:
        """Receives data from a request, until the desired size is reached.

        Args:
            total_size (int): The payload size in bytes to get.

        Returns:
            bytearray: The received data.
        """
        payload_data = bytearray()

        while total_size > len(payload_data):
            # Wait until the complete TCP payload was received.
            received = self.request.recv(total_size - len(payload_data))

            if 0 == len(received):
                # Give up, if no more data arrives.
                # Close the socket.
                self.request.shutdown(socket.SHUT_RDWR)
                self.request.close()

                raise ConnectionError

            payload_data.extend(received)

        return payload_data

    def handle_write_data(self, packet_header: bytes):
        """Handle requests, where SigmaStudio wants to write to the DSP.

        Args:
            packet_header (bytes): The binary request header, as received from SigmaStudio.
        """
        # This field indicates whether the packet is a block write or a safeload write.
        is_safeload = bytes_to_int8(packet_header, 1) == 1

        # This indicates the channel number.
        # TODO: This field is currently unused.
        channel_number = bytes_to_int8(packet_header, 2)
        del channel_number

        # This indicates the total length of the write packet (uint32).
        # This field is currently unused.
        total_length = bytes_to_int32(packet_header, 3)
        del total_length

        # The address of the chip to which the data has to be written.
        # The chip address is appended by the read/write bit: shifting right by one bit removes it.
        # TODO: This field is currently unused.
        chip_address = bytes_to_int8(packet_header, 7) >> 1
        del chip_address

        # The length of the data (uint32).
        total_payload_size = bytes_to_int32(packet_header, 8)

        # The address of the module whose data is being written to the DSP (uint16).
        address = bytes_to_int16(packet_header, 12)

        payload_data = self.receive_amount(total_payload_size)

        request: Union[WriteRequest, SafeloadRequest]

        if is_safeload:
            logger.info("[safeload] %s bytes to address 0x%04x", total_payload_size, address)
            request = SafeloadRequest(address, payload_data)
        else:
            logger.info("[write] %s bytes to address 0x%04x", total_payload_size, address)
            request = WriteRequest(address, payload_data)

        self.server.pipe_end_owner.send(request)

    def handle_read_request(self, packet_header: bytes):
        """Handle requests, where SigmaStudio wants to read from the DSP.

        Args:
            packet_header (bytes): The binary request header, as received from SigmaStudio.
        """
        # This indicates the total length of the read packet (uint32)
        total_length = bytes_to_int32(packet_header, 1)
        del total_length

        # The address of the chip from which the data has to be read
        chip_address = bytes_to_int8(packet_header, 5)

        # The length of the data (uint32)
        data_length = bytes_to_int32(packet_header, 6)

        # The address of the module whose data is being read from the DSP (uint16)
        address = bytes_to_int16(packet_header, 10)

        # Notify application of read request
        self.server.pipe_end_owner.send(ReadRequest(address, data_length))

        # Wait for payload data that goes into the read response
        read_response = self.server.pipe_end_owner.recv()

        payload_data = read_response.data

        # Build the read response packet, starting with length calculations ...
        total_transmit_length = SigmaStudioRequestHandler.HEADER_LENGTH + data_length
        transmit_data = bytearray(total_transmit_length)

        # ... followed by populating the byte fields.
        int8_to_bytes(
            SigmaStudioRequestHandler.COMMAND_READ_RESPONSE,
            transmit_data,
            0,
        )
        int32_to_bytes(total_transmit_length, transmit_data, 1)
        int8_to_bytes(chip_address, transmit_data, 5)
        int32_to_bytes(data_length, transmit_data, 6)
        int16_to_bytes(address, transmit_data, 10)

        # Success (0)/failure (1) byte at pos. 12. Always assume success.
        int16_to_bytes(0, transmit_data, 12)

        # Reserved zero byte at pos. 13
        int16_to_bytes(0, transmit_data, 13)

        transmit_data[SigmaStudioRequestHandler.HEADER_LENGTH :] = payload_data

        self.request.sendall(transmit_data)

    def handle(self):
        """Call, when the TCP server receives new data for handling.

        It never stops, except if the connection is reset.
        """
        while True:
            try:
                # Receive the packet header in the beginning.
                packet_header = self.receive_amount(SigmaStudioRequestHandler.HEADER_LENGTH)

                # The first byte of the header contains the command from SigmaStudio.
                command = packet_header[0]

                if command == SigmaStudioRequestHandler.COMMAND_WRITE:
                    self.handle_write_data(packet_header)

                elif command == SigmaStudioRequestHandler.COMMAND_READ:
                    self.handle_read_request(packet_header)

                else:
                    logging.info("Received an unknown command code '%s'.", hex(command))

            except ConnectionError:
                break
