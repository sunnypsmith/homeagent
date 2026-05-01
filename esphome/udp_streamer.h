#pragma once
#include "esphome.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"

/**
 * Streams raw PCM audio from the Atom Echo microphone to a central
 * voice service over UDP. Each packet is prefixed with a 4-byte room
 * ID so the server can demux multiple rooms on a single port.
 *
 * Includes:
 *   - Configurable gain amplification (PDM mics are very quiet by default)
 *   - DC offset removal via high-pass filter (removes metallic resonance)
 *   - Soft clipping to prevent distortion at high gain
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
static int MIC_GAIN = 8;

static const size_t MAX_AUDIO_CHUNK = 1400;
static const size_t ROOM_HEADER_SIZE = 4;
static const size_t MAX_SAMPLES = MAX_AUDIO_CHUNK / 2;  // 700

static uint8_t send_buf[4 + 1400];
static int16_t proc_buf[MAX_SAMPLES];

// DC offset removal — integer-only IIR high-pass (~50 Hz at 16 kHz)
// Uses Q15 fixed-point: alpha = 0.995 ≈ 32604/32768
static int32_t hp_prev_in = 0;
static int32_t hp_prev_out = 0;
static const int32_t HP_ALPHA_Q15 = 32604;

void udp_streamer_init(const char *server_ip, uint16_t server_port,
                       uint16_t listen_port, const char *room_id) {
  VOICE_SERVER_IP = server_ip;
  VOICE_SERVER_PORT = server_port;
  VOICE_LISTEN_PORT = listen_port;
  memset(ROOM_ID, 0, sizeof(ROOM_ID));
  strncpy(ROOM_ID, room_id, 4);
}

void udp_streamer_set_gain(int gain) {
  MIC_GAIN = gain;
  ESP_LOGI("udp", "Mic gain set to %d", gain);
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
  ESP_LOGI("udp", "Streaming to %s:%d, room=%s, gain=%d",
           VOICE_SERVER_IP, VOICE_SERVER_PORT, ROOM_ID, MIC_GAIN);
}

static inline int16_t soft_clip(int32_t sample) {
  if (sample > 32000) return 32000;
  if (sample < -32000) return -32000;
  return (int16_t)sample;
}

void stream_audio_udp(const std::vector<uint8_t> &data) {
  if (!udp_initialized || udp_sock < 0) return;

  const size_t total_samples = data.size() / 2;
  if (total_samples == 0) return;

  const int16_t *in = reinterpret_cast<const int16_t *>(data.data());
  const int32_t gain = MIC_GAIN;
  size_t offset = 0;

  while (offset < total_samples) {
    size_t chunk = total_samples - offset;
    if (chunk > MAX_SAMPLES) chunk = MAX_SAMPLES;

    for (size_t i = 0; i < chunk; i++) {
      int32_t s = (int32_t)in[offset + i];

      // Integer high-pass filter (Q15 fixed-point)
      int32_t hp = (HP_ALPHA_Q15 * (hp_prev_out + s - hp_prev_in)) >> 15;
      hp_prev_in = s;
      hp_prev_out = hp;

      // Gain + soft clip
      int32_t amp = hp * gain;
      if (amp > 32000) amp = 32000;
      else if (amp < -32000) amp = -32000;
      proc_buf[i] = (int16_t)amp;
    }

    size_t len = chunk * 2;
    memcpy(send_buf + ROOM_HEADER_SIZE, proc_buf, len);
    lwip_sendto(udp_sock, send_buf, ROOM_HEADER_SIZE + len, 0,
                (struct sockaddr *)&dest_addr, sizeof(dest_addr));
    offset += chunk;
  }
}
