# 필수 시스템 설정 규칙

SW Planner Agent가 수행해야 하는 필수 시스템 설정.  
**Ubuntu 기준으로 작성, Rocky 차이는 각 항목에 명시.**

모든 설정은 `sw_requirements.md`에 명시 여부와 무관하게 적용.  
설정 적용 후 재부팅은 별도 reboot 처리 정책(sw-install.md) 따름.

---

## 1. GRUB 커널 파라미터

### 공통 파라미터 (CPU 종류 무관)

```
iommu=pt
pcie_aspm=off
pcie_acs_override=downstream,multifunction
```

### CPU별 추가 파라미터

| CPU 제조사 | 추가 파라미터 |
|-----------|-------------|
| Intel | `intel_iommu=on intel_idle.max_cstate=0 processor.max_cstate=1` |
| AMD | `amd_iommu=on amd_pstate=passive` |

CPU 종류는 `lscpu | grep "Vendor ID"` 로 확인.

### 적용 방법

**Ubuntu:**
```bash
# /etc/default/grub 의 GRUB_CMDLINE_LINUX_DEFAULT 에 파라미터 추가
sudo sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 <파라미터>\"/' /etc/default/grub
sudo update-grub
```

**Rocky:**
```bash
# /etc/default/grub 의 GRUB_CMDLINE_LINUX 에 파라미터 추가
sudo grub2-mkconfig -o /boot/grub2/grub.cfg          # BIOS
sudo grub2-mkconfig -o /boot/efi/EFI/redhat/grub.cfg # UEFI
```

파라미터 추가 전 기존 값 확인 필수. 중복 추가 방지.

---

## 2. CPU 거버너 & 절전 비활성화

### CPU 거버너 performance 고정

**Ubuntu:**
```bash
sudo apt install -y linux-tools-$(uname -r) linux-tools-generic
sudo cpupower frequency-set -g performance
sudo systemctl enable --now cpupower.service
```

**Rocky:**
```bash
sudo dnf install -y kernel-tools
sudo cpupower frequency-set -g performance
sudo systemctl enable --now cpupower.service
```

영구 적용 확인: `cpupower frequency-info | grep "The governor"`

---

## 3. GPU 영구 모드 (Persistence Mode)

NVIDIA GPU가 탑재된 경우에만 적용. AMD/Tenstorrent는 해당 없음.

### PM 지원 여부 확인

```bash
sudo nvidia-smi -pm 1
```

- **exit code 0, 오류 없음**: PM 지원 → 아래 데몬 생성
- **"Not Supported" 메시지**: PM 미지원 → 이 항목 건너뜀

### 데몬 생성 (PM 지원 시)

```bash
cat <<'EOF' | sudo tee /etc/systemd/system/nvidia-power.service
[Unit]
Description=NVIDIA GPU Power Settings
After=nvidia-persistenced.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/nvidia-smi -pm 1
ExecStart=/usr/bin/nvidia-smi -pl 350
ExecStart=/usr/bin/nvidia-smi -lgc 1400,1400

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now nvidia-power.service
```

**Ubuntu / Rocky 동일.**

---

## 4. 자동 업데이트 방지

### Ubuntu

```bash
sudo systemctl disable --now unattended-upgrades
sudo systemctl disable --now apt-daily.timer
sudo systemctl disable --now apt-daily-upgrade.timer
sudo apt purge -y unattended-upgrades
```

커널 버전 고정 (현재 부팅 커널을 기본값으로 잠금):
```bash
sudo apt-mark hold linux-image-$(uname -r) linux-headers-$(uname -r)
```

### Rocky

```bash
sudo systemctl disable --now dnf-automatic.timer
sudo dnf remove -y dnf-automatic
```

커널 버전 고정:
```bash
sudo grubby --set-default /boot/vmlinuz-$(uname -r)
```

---

## 적용 순서

1. GRUB 파라미터 설정
2. CPU 거버너 설정
3. GPU 영구 모드 설정 (NVIDIA 탑재 시)
4. 자동 업데이트 방지
5. **재부팅** — GRUB 파라미터 반영을 위해 필수

재부팅 처리는 sw-install.md의 reboot 처리 패턴 따름 (300s SSH 재접속 폴링).

---

## 설정 검증

재부팅 후 확인 항목:

```bash
# GRUB 파라미터 적용 확인
cat /proc/cmdline

# CPU 거버너 확인
cpupower frequency-info | grep "The governor"

# GPU PM 상태 확인 (NVIDIA)
nvidia-smi --query-gpu=persistence_mode --format=csv,noheader

# 자동 업데이트 비활성화 확인 (Ubuntu)
systemctl is-enabled unattended-upgrades apt-daily.timer apt-daily-upgrade.timer
```
