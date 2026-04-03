# SW 설치 파이프라인 규칙

## 트리거 조건

- `jobs.sw_requirements` (Text) 값이 있으면 → SW Install 단계 실행
- 없으면 → SW Install 단계 통째로 skip, Post-install로 바로 이동
- `install_policy` 필드 없음. sw_requirements 유무만으로 분기.

---

## sw_requirements.md 파싱 규칙

엔지니어가 자유 형식 Markdown 불릿으로 작성. `sw_planner.py`가 파싱 후 설치계획 JSON으로 변환.

### 항목 분류

파싱 결과를 아래 4가지로 분류:

| 분류 | 처리 주체 | 예시 |
|------|----------|------|
| **SW 설치** | `sw_install.py` 스크립트 | `- CUDA 12.4`, `- PyTorch 2.3`, `- docker` |
| **계정 생성** | `sw_install.py` 스크립트 | `- 계정: user1 / pw: xxxx / sudo: yes` |
| **스토리지 마운트** | `sw_install.py` 스크립트 | `- 보조스토리지 /mnt/data 마운트` |
| **기타 시스템 설정** | SW Planner Agent 개입 | grub 파라미터, crontab, hibernate 설정 등 |

기타 시스템 설정이 감지되면 `sw_install.py`가 직접 실행하지 않고 반드시 `agent_gateway.py`를 통해 SW Planner Agent에 전달.

### 버전 명시 규칙

- 버전 명시 있음 → lookup table 조회 → 없으면 SW Planner Agent가 호환성 판단
- 버전 명시 없음 → 최신 버전 설치 (apt/pip 기본 동작)

---

## 버전 호환 Lookup Table

파일 위치: `config/sw_compat_matrix.json`

```json
{
  "nvidia_driver": {
    "570": { "cuda": ["12.8"], "gcc_min": "11" },
    "560": { "cuda": ["12.6", "12.7"], "gcc_min": "11" },
    "550": { "cuda": ["12.4", "12.5"], "gcc_min": "12" }
  },
  "cuda": {
    "12.8": { "torch": ["2.6"], "cudnn": ["9.x"] },
    "12.6": { "torch": ["2.4", "2.5"], "cudnn": ["9.x"] },
    "12.4": { "torch": ["2.3"], "cudnn": ["9.x"] }
  }
}
```

- 유저 요구 조합이 테이블에 있음 → 스크립트 직접 설치
- 테이블에 없음 → SW Planner Agent가 호환성 판단 후 설치계획 JSON 반환

---

## 설치 순서 및 의존성

```
[gcc-12/g++-12]          ← nvidia-driver 550+ 설치 전 필수
        ↓
[nvidia-driver]          ← apt (cuda-keyring 경유)
        ↓ REBOOT
[CUDA toolkit]           ← apt (cuda-keyring 경유) + .bashrc PATH 추가
[cuDNN]                  ← apt (cuda-keyring 경유)
        ↓
[torch / 프레임워크]      ← pip 또는 conda 환경
[NCCL]                   ← torch 설치 시 포함되거나 별도 빌드

[docker]                 ← 공식 GPG key + repo 등록 후 apt
        ↓
[docker-container-toolkit] ← docker 설치 선행 필수

[miniconda]              ← wget으로 설치 스크립트 다운로드 후 실행

[python (비기본 버전)]   ← deadsnakes PPA → apt

[tt-kmd]                 ← git clone → apt install dkms → make dkms
        ↓
[rustup + cargo]         ← tt-smi 설치 전 필수
[tt-smi]                 ← pip install tt-smi
[tt-burnin]              ← git clone → pip3 install .
```

의존성 위반 시 설치 실패로 마킹. 재시도 없음.

---

## OS별 처리 방침

- **Ubuntu 22.04 / 24.04**: 스크립트 직접 처리 (apt, cuda-keyring, 공식 절차)
- **Rocky 9/10, RHEL 9, Debian 계열, Fedora 계열**: SW Planner Agent 판단
  - Agent는 OS를 먼저 감지(`/etc/os-release`)하고 적합한 설치 방법 결정
  - 추후 Rocky 전용 스크립트 추가 예정

OS 감지는 SSH 접속 후 `cat /etc/os-release`로 수행. `ID`, `VERSION_ID` 필드 파싱.

---

## 항목별 설치 절차 및 최소 검증

### nvidia-driver

```bash
# 1. cuda-keyring 설치
wget https://developer.download.nvidia.com/compute/cuda/repos/{os}/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt update

# 2. gcc-12 선행 (driver 550+)
sudo apt install -y gcc-12 g++-12
sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 12
sudo update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 12

# 3. driver 설치
sudo apt install -y nvidia-driver-<version>

# 4. reboot (아래 Reboot 처리 패턴 참조)
```

검증: reboot 후 `nvidia-smi` 실행 → exit code 0 + GPU 인식 확인

---

### CUDA toolkit

```bash
sudo apt install -y cuda-toolkit-<version>  # 예: cuda-toolkit-12-4

# .bashrc PATH 추가 (SSH 접속 유저 기준)
echo 'export PATH=/usr/local/cuda/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
```

검증: `nvcc --version` → exit code 0

---

### cuDNN

```bash
sudo apt install -y cudnn  # cuda-keyring 등록 후
```

검증: `dpkg -l | grep cudnn` → 패키지 존재 확인

---

### PyTorch / 프레임워크

```bash
# system pip
pip install torch==<version> torchvision torchaudio --index-url https://download.pytorch.org/whl/cu<cuda_ver>

# conda 환경이 있을 경우
conda run -n <env> pip install torch==<version> ...
```

검증: `python3 -c "import torch; print(torch.cuda.is_available())"` → `True`

---

### docker

```bash
sudo apt update && sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl start docker
```

검증: `sudo docker run hello-world` → exit code 0

---

### docker-container-toolkit

docker 설치 선행 필수.

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update
export NVIDIA_CONTAINER_TOOLKIT_VERSION=1.19.0-1
sudo apt install -y \
  nvidia-container-toolkit=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
  nvidia-container-toolkit-base=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
  libnvidia-container-tools=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
  libnvidia-container1=${NVIDIA_CONTAINER_TOOLKIT_VERSION}
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

검증: `sudo docker run --rm --runtime=nvidia --gpus all ubuntu nvidia-smi` → GPU 목록 출력

---

### miniconda

silent mode 설치 (`-b`: batch, 라이선스 동의 포함 모든 프롬프트 자동 수락).

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh

# -b: silent/batch mode (no prompts)
# -p: 설치 경로 지정
# -u: 이미 설치되어 있으면 업데이트
bash /tmp/miniconda.sh -b -u -p ~/miniconda3

~/miniconda3/bin/conda init bash
```

검증: 새 bash 세션에서 `conda list` → exit code 0 (base 환경 자동 활성화)

설치 경로: `~/miniconda3` (SSH 접속 유저 홈 디렉토리)

---

### python (비기본 버전)

Ubuntu 전용.

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.<minor>
```

검증: `python3.<minor> --version` → exit code 0

---

### gcc-12 / g++-12

```bash
sudo apt install -y gcc-12 g++-12
sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 12
sudo update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 12
```

검증: `gcc --version` → `gcc 12.x` 확인

---

### tt-kmd

```bash
git clone https://github.com/tenstorrent/tt-kmd.git ~/tt-kmd
cd ~/tt-kmd
sudo apt install -y dkms
make dkms
```

검증: `ls /dev/tenstorrent` → 파일 존재 확인

---

### tt-smi

rustup 선행 필수.

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
pip install tt-smi
```

검증: `tt-smi -ls` → tenstorrent 가속기 목록 출력

---

### tt-burnin

tt-kmd 설치 선행 필수.

```bash
git clone https://github.com/tenstorrent/tt-burnin.git ~/tt-burnin
cd ~/tt-burnin
pip3 install --upgrade pip
pip3 install .
```

검증: `tt-burnin --help` → exit code 0

---

## Reboot 처리 패턴

nvidia-driver 설치 후 reboot이 필요한 유일한 항목.

```
1. sudo reboot 실행
2. SSH 연결 종료 감지
3. 최대 300s 동안 SSH 재접속 폴링 (10s 간격)
4. 재접속 성공 → nvidia-smi 검증
5. 300s 초과 → FAIL 마킹 (reboot_timeout)
```

Celery task는 reboot 전 상태를 DB에 `rebooting` 으로 기록하고 폴링 루프로 재접속 대기.  
재접속 후 동일 task가 이어서 실행 (새 task 생성 아님).

---

## 계정 생성

sw_requirements.md에 계정 생성 항목이 있을 때 처리.

```bash
sudo useradd -m -s /bin/bash <username>
echo "<username>:<password>" | sudo chpasswd
# sudo 권한 요구 시
sudo usermod -aG sudo <username>
```

- 패스워드는 sw_requirements.md에서 평문으로 수신 → SecretStr 처리, 로그 마스킹, DB 미저장
- 계정 생성 후 검증: `id <username>` → exit code 0

---

## 스토리지 마운트 (fstab)

sw_requirements.md에 마운트 요구가 명시된 경우에만 실행.  
명시 없으면 마운트되지 않은 보조스토리지가 있어도 건드리지 않음.

```bash
# 마운트되지 않은 보조 디스크 탐색
lsblk -rno NAME,MOUNTPOINT,TYPE | grep disk

# ext4 포맷
sudo mkfs.ext4 /dev/<device>

# 마운트 포인트 생성
sudo mkdir -p /mnt/data

# fstab 등록 전 백업
sudo cp /etc/fstab /etc/fstab.bak

# fstab 추가
UUID=$(sudo blkid -s UUID -o value /dev/<device>)
echo "UUID=${UUID} /mnt/data ext4 defaults 0 2" | sudo tee -a /etc/fstab

# 마운트 확인
sudo mount -a
```

검증: `mountpoint /mnt/data` → exit code 0  
실패 시: `/etc/fstab.bak` 복구 + 실패 마킹 + 로그

---

## 실패 처리

- 롤백 없음
- 설치 실패 시: 해당 항목 실패 마킹 + 로그 파일 출력 (`/srv/inspection/results/{job_id}/sw_install.log`)
- 의존성 위반으로 인한 후속 항목은 `skipped_due_to_dependency` 로 마킹
- 전체 job은 실패 항목이 있어도 계속 진행 (후속 단계에서 영향 범위 판단)
- Agent 개입 후에도 복구 실패 시 → `sw_install_failed` 상태로 job 마킹, post_install 진행

---

## SW Planner Agent 호출 조건 요약

| 조건 | 예시 |
|------|------|
| 버전 조합이 lookup table에 없음 | `driver 565 + cuda 12.5 + torch 2.4` |
| 기타 시스템 설정 항목 감지 | grub 파라미터, crontab, hibernate |
| Ubuntu 이외 OS | Rocky 9, RHEL 9, Fedora 계열 |
| 설치 실패 후 복구 판단 필요 | 의존성 충돌, 알 수 없는 에러 |
