#pragma once
#include "esphome.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"

/**
 * Streams raw PCM audio from the Atom Echo microphone to a central
 * voice service over UDP. Each packet is prefixed with a 4-byte room
 * ID so the server can demux multiple rooms on a single port.
 *
 * Packet format:
 *   [4 bytes room_id][N bytes PCM audio (16-bit LE mono 16kHz)]
 */

static int udp_sock = -1;
static struct sockaddr_in dest_addr;
static bool udp_initialized = false;

static const char *VOICE_SERVER_IP = nullptr;
static uint16_t VOICE_SERVER_PORT = 0;
static uint16_t VOICE_LISTEN_PORT = 0;
static char ROOM_ID[5] = {0};

static const size_t MAX_AUDIO_CHUNK = 1400;
static const size_t ROOM_HEADER_SIZE = 4;

static uint8_t send_buf[4 + 1400];

void udp_streamer_init(const char *server_ip, uint16_t server_port,
                       uint16_t listen_port, const char *room_id) {
  VOICE_SERVER_IP = server_ip;
  VOICE_SERVER_PORT = server_port;
  VOICE_LISTEN_PORT = listen_port;
  memset(ROOM_ID, 0, sizeof(ROOM_ID));
  strncpy(ROOM_ID, room_id, 4);
}

void udp_streamer_start() {
  if (udp_initialized) return;

  udp_sock = lwip_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (udp_sock < 0) {
    ESP_LOGE("udp", "Failed to create socket");
    return;
  }

  memset(&dest_addr, 0, sizeof(dest_addr));
  dest_addr.sin_family = AF_INET;
  dest_addr.sin_port = htons(VOICE_SERVER_PORT);
  inet_aton(VOICE_SERVER_IP, &dest_addr.sin_addr);

  memcpy(send_buf, ROOM_ID, ROOM_HEADER_SIZE);

  udp_initialized = true;
  ESP_LOGI("udp", "Streaming to %s:%d, room=%s",
           VOICE_SERVER_IP, VOICE_SERVER_PORT, ROOM_ID);
}

void stream_audio_udp(const std::vector<uint8_t> &data) {
  if (!udp_initialized || udp_sock < 0) return;

  for (size_t i = 0; i < data.size(); i += MAX_AUDIO_CHUNK) {
    size_t len = std::min(MAX_AUDIO_CHUNK, data.size() - i);
    memcpy(send_buf + ROOM_HEADER_SIZE, data.data() + i, len);
    lwip_sendto(udp_sock, send_buf, ROOM_HEADER_SIZE + len, 0,
                (struct sockaddr *)&dest_addr, sizeof(dest_addr));
  }
}
