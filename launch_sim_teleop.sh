#!/bin/bash
# ============================================================================
# Sim2Sim Teleoperation - All-in-One Launcher
# ============================================================================
# Usage:
#   ./launch_sim_teleop.sh              # Launch with keyboard control
#   ./launch_sim_teleop.sh --teleop     # Launch with PICO VR teleop
#   ./launch_sim_teleop.sh --no-cam     # Skip camera stream server
#   ./launch_sim_teleop.sh --stop       # Stop all running processes
#
# tmux window switching:  Ctrl+B → 0/1/2/3
# tmux detach:            Ctrl+B → d
# tmux re-attach:         tmux attach -t sim
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

unset CYCLONEDDS_URI

SESSION="sim"
VENV_SIM="$SCRIPT_DIR/.venv_sim/bin/activate"
VENV_TELEOP="$SCRIPT_DIR/.venv_teleop/bin/activate"
ENABLE_CAMERA=true
TELEOP_MODE=false

for arg in "$@"; do
    case $arg in
        --teleop) TELEOP_MODE=true ;;
        --no-cam) ENABLE_CAMERA=false ;;
        --stop)
            tmux kill-session -t "$SESSION" 2>/dev/null && echo "Stopped." || echo "No active session."
            exit 0
            ;;
        -h|--help) head -14 "$0" | tail -12; exit 0 ;;
    esac
done

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Killing existing session..."
    tmux kill-session -t "$SESSION"
    sleep 1
fi

if [ "$TELEOP_MODE" = true ]; then
    DEPLOY_EXTRA_ARGS="--input-type zmq_manager"
    MODE_LABEL="PICO VR Teleop (zmq_manager)"
else
    DEPLOY_EXTRA_ARGS=""
    MODE_LABEL="Keyboard (manager)"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║          Sim2Sim Teleoperation Launcher                     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "  Mode: $MODE_LABEL"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Window 0: MuJoCo Simulator
echo "[1/4] MuJoCo Simulator..."
tmux new-session -d -s "$SESSION" -n "0:sim" \
    "bash -c 'unset CYCLONEDDS_URI && source \"$VENV_SIM\" && echo \"=== MuJoCo Simulator ===\" && python gear_sonic/scripts/run_sim_loop.py --enable-offscreen --enable-image-publish; echo \"[Exited]\"; read'"

sleep 3

# Window 1: C++ Deploy
echo "[2/4] C++ Deploy..."
tmux new-window -t "${SESSION}:" -n "1:deploy" \
    "bash -c 'unset CYCLONEDDS_URI && echo \"=== C++ Deploy ===\" && cd \"$SCRIPT_DIR\" && bash gear_sonic_deploy/deploy.sh sim -y $DEPLOY_EXTRA_ARGS; echo \"[Exited] Press any key\"; read'"

# Window 2: Camera Stream
if [ "$ENABLE_CAMERA" = true ]; then
    if [ "$TELEOP_MODE" = true ]; then
        echo "[3/4] Camera → PICO (Orin Video Sender protocol)..."
        tmux new-window -t "${SESSION}:" -n "2:camera" \
            "bash -c 'source \"$VENV_SIM\" && cd \"$SCRIPT_DIR\" && echo \"=== MuJoCo Video Sender ===\" && python gear_sonic/scripts/mujoco_video_sender.py --camera head_camera; echo \"[Exited]\"; read'"
    else
        echo "[3/4] Camera → HTTP..."
        tmux new-window -t "${SESSION}:" -n "2:camera" \
            "bash -c 'source \"$VENV_SIM\" && cd \"$SCRIPT_DIR\" && echo \"=== Camera Stream Server ===\" && python gear_sonic/scripts/camera_stream_server.py; echo \"[Exited]\"; read'"
    fi
else
    echo "[3/4] Camera skipped"
fi

# Window 3: PICO Manager
echo "[4/4] PICO Manager..."
tmux new-window -t "${SESSION}:" -n "3:pico" \
    "bash -c 'source \"$VENV_TELEOP\" && cd \"$SCRIPT_DIR\" && echo \"=== PICO VR Manager ===\" && python gear_sonic/scripts/pico_manager_thread_server.py --manager; echo \"[Exited]\"; read'"

# Focus on deploy window (needs user confirmation)
tmux select-window -t "${SESSION}:1:deploy"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo " Window 0: MuJoCo Sim     | Window 1: Deploy (← Y+Enter)"
echo " Window 2: Camera Stream  | Window 3: PICO Manager"
echo "═══════════════════════════════════════════════════════════════"
echo ""

if [ "$TELEOP_MODE" = true ]; then
    echo " [PICO VR テレオペモード]"
    echo ""
    echo " 手順:"
    echo "   1. deploy で Y+Enter (確認) → ] (制御開始)"
    echo "   2. MuJoCo ビューワーで 9 キー (ElasticBand 解除)"
    echo "   3. PICO: XRoboToolkit → Remote Vision → ZEDMINI → IP入力 → Confirm"
    echo "   4. PICO で T-ポーズ → A+B+X+Y (ポリシー起動 + キャリブ)"
    echo "   5. A+X (POSE モード: 全身テレオペ)"
    echo "   6. A+X で PLANNER に戻る"
    echo "   7. A+B+X+Y で停止"
    echo ""
    echo " PICO コントロール:"
    echo "   A+B+X+Y   = ポリシー起動/停止"
    echo "   A+X        = PLANNER <-> POSE 切替"
    echo "   B+Y        = PLANNER_FROZEN_UPPER <-> POSE"
    echo "   L-Stick    = VR_3PT (PLANNER時)"
    echo "   左スティック = 移動方向 (PLANNER時)"
    echo "   右スティック = 旋回 (PLANNER時)"
    echo "   A+B / X+Y  = ロコモーション切替"
    echo "   トリガー    = ハンド開閉"
else
    echo " [キーボード操作モード]"
    echo ""
    echo " 手順:"
    echo "   1. deploy で Y+Enter (確認)"
    echo "   2. deploy で ] (制御ループ開始)"
    echo "   3. deploy で Enter (プランナー有効化)"
    echo "   4. deploy で 1 (SLOW_WALK モード選択)"
    echo "   5. w/s=前後 a/d=旋回 q/e=方向転換"
    echo ""
    echo " MuJoCo ビューワー:"
    echo "   9 = ElasticBand ON/OFF (ロボット昇降)"
fi

echo ""
echo " Ctrl+B → 0/1/2/3 でウィンドウ切り替え"
echo " Ctrl+B → d でデタッチ | ./launch_sim_teleop.sh --stop で全停止"
echo "═══════════════════════════════════════════════════════════════"
echo ""

tmux attach -t "$SESSION"
