# 검수 대상 서버 — 제품군

> 검수 환경(SSH 접근 방식·OS 상태·네트워크) → `context/infra-environment.md`

## 회사 / 브랜드

- 회사: ManyCoreSoft
- 서버 브랜드: DeepGadget (`deepgadget.com`)

---

## 제품 라인업

### 현행 (신규 출고 + RMA)

| 제품 | 폼팩터 | 비고 |
|------|--------|------|
| **dg5W** | 워크스테이션 (Tower/5U) | 주력 |
| **dg5R** | 랙마운트 (6U) | 고성능 |
| **dg5W-TT** | 워크스테이션 (Tower) | Tenstorrent 전용 구성 |

### 단종 (RMA만 진행)

| 제품 | 폼팩터 |
|------|--------|
| dg3S | 스탠드얼론 서버 |
| dg3R | 랙마운트 서버 |
| dg4W | 워크스테이션 |
| dg4R | 랙마운트 서버 |
| dg4F | 랙마운트 고성능 서버 |

단종 제품도 `gpu_server` 프로파일 하나로 커버.

---

## 제품별 스펙

### dg5W (워크스테이션 — 주력)

| 항목 | 스펙 |
|------|------|
| 마더보드 | ASUS Pro WS WRX90E-SAGE SE |
| CPU | AMD Ryzen Threadripper PRO 9000 Series (단일 소켓) |
| GPU | 최대 8장 (PCIe 5.0 x16 × 7슬롯) |
| RAM | 최대 768GB DDR5-5600 ECC |
| 주 스토리지 | M.2 NVMe SSD 최대 4TB |
| 보조 스토리지 | M.2 NVMe / 2.5" SATA / U.2 NVMe, 최대 64TB |
| 기본 NIC | 2× 10GbE Base-T + IPMI 1Gbps |
| 옵션 NIC | InfiniBand NDR/HDR/EDR, 400G/200G/100G Ethernet |
| 전원 | 4× 2,000W 핫스왑 이중화 (80+ Platinum, 총 8kW) |
| 폼팩터 | 445×780×225mm (5U Rack 전환 가능) |

### dg5R (랙마운트 — 고성능)

| 항목 | 스펙 |
|------|------|
| 마더보드 (Intel) | ASRock Rack GNR2D32G-2L+, GNRAP2D24G-2L+ |
| 마더보드 (AMD) | ASRock Rack TURIN2D24G-2L+/500W |
| CPU | 듀얼 소켓 Intel Xeon 6700/6900 또는 AMD EPYC 9005 |
| GPU | 최대 10장 (PCIe 5.0 x16 × 15슬롯) |
| RAM | DDR5-6400 RDIMM ECC, 최대 4,096GB (Intel 6700) / 3,072GB (6900·EPYC) |
| 주 스토리지 | 4× M.2 NVMe SSD (최대 8TB, HW RAID) |
| 보조 스토리지 | U.2 NVMe 최대 16개 (최대 983TB) |
| 기본 NIC | 2× 1GbE Base-T + IPMI 1Gbps |
| 옵션 NIC | NVIDIA ConnectX-6 (IB EDR/HDR200 또는 200GbE), ConnectX-7 (IB NDR 또는 400GbE) |
| 전원 | 4× 3,200W 핫스왑 이중화 (80+ Titanium, 총 12.8kW) |
| 폼팩터 | 447×911.5×266mm (6U) |

### dg5W-TT (Tenstorrent 전용)

| 항목 | 스펙 |
|------|------|
| CPU | AMD EPYC 9124 |
| 가속기 | 4× Tenstorrent Wormhole n300 |
| RAM | 4× 64GB DDR5-5600 ECC/REG (256GB) |
| 스토리지 | 1TB M.2 NVMe SSD |
| 기본 NIC | 2× 10GbE Base-T + IPMI 1Gbps |
| 옵션 NIC | 100G/200G Dual NIC |
| 전원 | 4× 1,300W 핫스왑 이중화 (80+ Platinum, 총 5.2kW) |

---

## 탑재 가속기 목록

NVIDIA 계열이 주력. 대부분의 NVIDIA GPU 탑재 가능.

### NVIDIA (주력)

| GPU | 주 탑재 제품 |
|-----|-------------|
| RTX PRO 6000 Blackwell (Server Edition) | dg5W, dg5R |
| RTX 5090 | dg5W, dg5R |
| RTX 6000 Ada | dg5W |
| L40S | dg5W |
| H200 NVL | dg5W, dg5R |
| A100 / A100X | dg5R (및 단종 제품) |

### Tenstorrent

| 칩 | 모델 | 주 탑재 제품 |
|----|------|-------------|
| Wormhole | n300s | dg5W-TT, dg5W, dg5R |
| Blackhole | p150a | dg5R |

### 기타

| 제조사 | 모델 | 주 탑재 제품 |
|--------|------|-------------|
| AMD | RX 9070 XT, AI PRO R9700 | dg5R |
| FuriosaAI | RNGD, WARBOY | dg5R |

---

## 프로파일

전 제품군 공통: `checks/profiles/gpu_server.json`

Tenstorrent 가속기 탑재 시 tt-kmd/tt-smi/tt-burnin 설치가 `sw_requirements.md`에 명시되어 SW Install 단계에서 처리.
