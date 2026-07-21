#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

TARGET_ALL=false
TARGET_NODE=""
JSON_OUTPUT=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Diagnose health of k3s cluster nodes.

OPTIONS:
  --node <name>    Debug a specific node
  --all            Show summary for all nodes
  --json           Output in JSON format
  -h, --help       Show this help message

EXAMPLES:
  $(basename "$0") --node mynode
  $(basename "$0") --all
  $(basename "$0") --all --json
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --all)
            TARGET_ALL=true
            shift
            ;;
        --node)
            TARGET_NODE="$2"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            usage
            ;;
    esac
done

require_tools kubectl tailscale jq

TARGET_NAMES=()

if [[ "$TARGET_ALL" == "true" ]]; then
    get_all_nodes
    TARGET_NAMES=("${NODE_NAMES[@]}")
elif [[ -n "$TARGET_NODE" ]]; then
    get_all_nodes
    for name in "${NODE_NAMES[@]}"; do
        if [[ "$name" == "$TARGET_NODE" ]]; then
            TARGET_NAMES=("$name")
            break
        fi
    done
    if [[ ${#TARGET_NAMES[@]} -eq 0 ]]; then
        echo "Error: Node '$TARGET_NODE' not found." >&2
        exit 1
    fi
else
    select_node
    TARGET_NAMES=("$SELECTED_NODE_NAME")
fi

status_indicator() {
    local status="$1"
    local good="$2"
    if [[ "$status" == "$good" ]]; then
        echo "[SUCCESS]"
    else
        echo "[ERROR]"
    fi
}

diagnose_node() {
    local hostname="$1"
    local role
    role=$(get_node_role "$hostname")

    local reachable="yes"
    if is_local_node "$hostname"; then
        reachable="yes"
    elif ! $SSH_CMD "${SSH_USER}@$hostname" "echo ok" >/dev/null
    then
        reachable="no"
    fi

    local node_ready="N/A"
    node_ready=$(kubectl get node "$hostname" \
        -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' \
        2>/dev/null || echo "N/A")

    local service_name
    service_name=$(get_k3s_service_name "$hostname")
    local service_status="N/A"
    local reboot_required="N/A"
    local load_avg="N/A"
    local memory="N/A"
    local disk="N/A"
    local drive_health="N/A"
    local io_errors=""
    local cpu_temp="N/A"
    local battery="N/A"
    local mem_errors="N/A"
    local failed_units="N/A"

    if [[ "$reachable" == "yes" ]]; then
        service_status=$(run_on_node "$hostname" \
            "systemctl is-active $service_name" \
            2>/dev/null || echo "failed")

        reboot_required=$(check_reboot_required "$hostname")

        load_avg=$(run_on_node "$hostname" \
            "cat /proc/loadavg | awk '{print \$1, \$2, \$3}'" \
            2>/dev/null || echo "N/A")

        memory=$(run_on_node "$hostname" \
            "free -b | awk '/^Mem:/ {printf \"%.1fGi / %.1fGi (%.1f%% used)\", \$3/1073741824, \$2/1073741824, \$3/\$2*100}'" \
            2>/dev/null || echo "N/A")
        if [[ "$memory" == "" ]]; then
            memory=$(run_on_node "$hostname" \
                "free -h | awk '/^Mem:/ {print \$3, \"/\", \$2}'" \
                2>/dev/null || echo "N/A")
        fi

        disk=$(run_on_node "$hostname" \
            "df -h --output=target,size,used,pcent -x tmpfs -x devtmpfs -x squashfs -x overlay -x proc -x sysfs -x fuse.snapfuse -x efivarfs 2>/dev/null | awk 'NR>1 {printf \"%s: %s / %s (%s)\\n\", \$1, \$3, \$2, \$4}'" \
            2>/dev/null || echo "N/A")

        drive_health=$(run_on_node_sudo "$hostname" "
            disks=\$(lsblk -dn -o NAME,TYPE | grep disk | awk '{print \$1}')
            if [ -z \"\$disks\" ]; then
                echo \"no_disks_found\"
            else
                res=\"\"
                for name in \$disks; do
                    dev=\"/dev/\$name\"
                    case \"\$name\" in
                        mmcblk*)
                            eol=\$(cat /sys/block/\$name/device/pre_eol_info 2>/dev/null)
                            life=\$(cat /sys/block/\$name/device/life_time 2>/dev/null)
                            case \"\$eol\" in
                                0x01|1) status=\"PASSED\" ;;
                                0x02|2) status=\"WARNING\" ;;
                                0x03|3) status=\"FAILED\" ;;
                                *) status=\"UNKNOWN\" ;;
                            esac
                            if [ -n \"\$life\" ] && [ \"\$status\" != \"UNKNOWN\" ]; then
                                status=\"\$status(life:\$life)\"
                            fi
                            ;;
                        *)
                            if ! command -v smartctl >/dev/null 2>&1; then
                                status=\"smartctl_missing\"
                            else
                                status=\$(smartctl -H \$dev 2>/dev/null | grep -E 'self-assessment|SMART Health Status' | awk '{print \$NF}')
                                [ -z \"\$status\" ] && status=\"UNKNOWN\"
                            fi
                            ;;
                    esac
                    res=\"\$res \$dev:\$status\"
                done
                echo \"\$res\" | sed 's/^ //'
            fi
        " 2>/dev/null || echo "N/A")

        io_errors=$(run_on_node_sudo "$hostname" \
            "dmesg | grep -iE 'i/o error|buffer i/o error' | tail -n 3" \
            2>/dev/null || echo "")

        cpu_temp=$(run_on_node "$hostname" "
            found=''
            for label_file in /sys/class/hwmon/hwmon*/temp*_label; do
                [ -f \"\$label_file\" ] || continue
                label=\$(cat \"\$label_file\" 2>/dev/null)
                case \"\$label\" in
                    'Package id 0'|'Package id 1')
                        input=\"\${label_file%_label}_input\"
                        val=\$(cat \"\$input\" 2>/dev/null) || continue
                        found=\"\$(( val / 1000 ))C\"
                        break
                        ;;
                esac
            done
            if [ -n \"\$found\" ]; then
                echo \"\$found\"
            elif [ -f /sys/class/thermal/thermal_zone0/temp ]; then
                raw=\$(cat /sys/class/thermal/thermal_zone0/temp)
                echo \"\$(( raw / 1000 ))C\"
            else
                echo 'N/A'
            fi
        " 2>/dev/null || echo "N/A")

        battery=$(run_on_node "$hostname" "
            if [ -d /sys/class/power_supply/BAT0 ]; then
                status=\$(cat /sys/class/power_supply/BAT0/status 2>/dev/null || echo 'Unknown')
                capacity=\$(cat /sys/class/power_supply/BAT0/capacity 2>/dev/null || echo '?')
                health='N/A'
                if [ -f /sys/class/power_supply/BAT0/health ]; then
                    health=\$(cat /sys/class/power_supply/BAT0/health)
                elif [ -f /sys/class/power_supply/BAT0/energy_full ] \
                  && [ -f /sys/class/power_supply/BAT0/energy_full_design ]; then
                    full=\$(cat /sys/class/power_supply/BAT0/energy_full)
                    design=\$(cat /sys/class/power_supply/BAT0/energy_full_design)
                    if [ \"\$design\" -gt 0 ] 2>/dev/null; then
                        pct=\$(( full * 100 / design ))
                        health=\"\${pct}% of design\"
                    fi
                fi
                echo \"\${capacity}% (\${status}), health: \${health}\"
            elif [ -d /sys/class/power_supply/BAT1 ]; then
                status=\$(cat /sys/class/power_supply/BAT1/status 2>/dev/null || echo 'Unknown')
                capacity=\$(cat /sys/class/power_supply/BAT1/capacity 2>/dev/null || echo '?')
                echo \"\${capacity}% (\${status})\"
            else
                echo 'no_battery'
            fi
        " 2>/dev/null || echo "N/A")

        mem_errors=$(run_on_node "$hostname" "
            errs=0
            for mc in /sys/devices/system/edac/mc/mc*; do
                [ -d \"\$mc\" ] || continue
                ce=\$(cat \"\$mc/ce_count\" 2>/dev/null || echo 0)
                ue=\$(cat \"\$mc/ue_count\" 2>/dev/null || echo 0)
                errs=\$((errs + ce + ue))
            done
            hw=\$(dmesg 2>/dev/null \
                | grep -ciE \
                'hardware error|memory.*error|mce:.*bank|corrected error' \
                || echo 0)
            errs=\$((errs + hw))
            if [ \"\$errs\" -gt 0 ]; then
                echo \"\${errs} errors\"
            else
                echo 'none'
            fi
        " 2>/dev/null || echo "N/A")

        failed_units=$(run_on_node "$hostname" \
            "systemctl --failed --no-legend --no-pager" \
            2>/dev/null || echo "")
        if [[ -z "$failed_units" ]]; then
            failed_units="none"
        fi
    fi

    local pod_summary="N/A"
    local unhealthy_count=0
    local total_pods=0
    if [[ "$node_ready" == "True" ]]; then
        local pods_output
        pods_output=$(kubectl get pods --all-namespaces \
            --field-selector "spec.nodeName=$hostname" \
            --no-headers 2>/dev/null || echo "")
        if [[ -n "$pods_output" ]]; then
            total_pods=$(echo "$pods_output" | wc -l)
            unhealthy_count=$(echo "$pods_output" \
                | grep -cv -E "Running|Completed" || true)
        fi
        pod_summary="${total_pods} total, ${unhealthy_count} unhealthy"
    fi

    if [[ "$JSON_OUTPUT" == "true" ]]; then
        jq -n \
            --arg hostname "$hostname" \
            --arg role "$role" \
            --arg reachable "$reachable" \
            --arg node_ready "$node_ready" \
            --arg service_status "$service_status" \
            --arg reboot_required "$reboot_required" \
            --arg load_avg "$load_avg" \
            --arg memory "$memory" \
            --arg disk "$disk" \
            --arg drive_health "$drive_health" \
            --arg io_errors "$io_errors" \
            --arg cpu_temp "$cpu_temp" \
            --arg battery "$battery" \
            --arg mem_errors "$mem_errors" \
            --arg failed_units "$failed_units" \
            --arg pod_summary "$pod_summary" \
            '{
                hostname: $hostname,
                role: $role,
                reachable: $reachable,
                node_ready: $node_ready,
                service_status: $service_status,
                reboot_required: $reboot_required,
                load_avg: $load_avg,
                memory: $memory,
                disk: $disk,
                drive_health: $drive_health,
                io_errors: $io_errors,
                cpu_temp: $cpu_temp,
                battery: $battery,
                mem_errors: $mem_errors,
                failed_units: $failed_units,
                pods: $pod_summary
            }'
        return
    fi

    echo "=== Node: $hostname ($role) ==="
    echo ""

    local ts_ind
    ts_ind=$(status_indicator "$reachable" "yes")
    echo "Tailscale:          $ts_ind $reachable"

    local ready_ind
    ready_ind=$(status_indicator "$node_ready" "True")
    echo "k3s Node Status:    $ready_ind $node_ready"

    local svc_ind
    svc_ind=$(status_indicator "$service_status" "active")
    echo "k3s Service:        $svc_ind $service_status"

    local drive_ind="[SUCCESS]"
    if [[ "$drive_health" == *"FAILED"* ]] || [[ -n "$io_errors" ]]; then
        drive_ind="[ERROR]"
    elif [[ "$drive_health" == *"WARNING"* ]] \
        || [[ "$drive_health" == *"UNKNOWN"* ]] \
        || [[ "$drive_health" == *"smartctl_missing"* ]]; then
        drive_ind="[WARNING]"
    fi
    echo "Drive Health:       $drive_ind $drive_health"
    if [[ -n "$io_errors" ]]; then
        echo "  [WARNING] Recent I/O errors detected in dmesg:"
        echo "$io_errors" | sed 's/^/    /'
    fi

    local mem_ind="[SUCCESS]"
    if [[ "$mem_errors" == *"errors"* ]]; then
        mem_ind="[ERROR]"
    elif [[ "$mem_errors" == "N/A" ]]; then
        mem_ind="[WARNING]"
    fi
    echo "Memory Health:      $mem_ind $mem_errors"

    local temp_ind="[SUCCESS]"
    if [[ "$cpu_temp" != "N/A" ]]; then
        local temp_val="${cpu_temp%C}"
        if [[ "$temp_val" =~ ^[0-9]+$ ]] && [[ "$temp_val" -ge 80 ]]; then
            temp_ind="[ERROR]"
        elif [[ "$temp_val" =~ ^[0-9]+$ ]] && [[ "$temp_val" -ge 65 ]]; then
            temp_ind="[WARNING]"
        fi
    else
        temp_ind="[WARNING]"
    fi
    echo "CPU Temperature:    $temp_ind $cpu_temp"

    if [[ "$battery" != "no_battery" && "$battery" != "N/A" ]]; then
        local bat_ind="[SUCCESS]"
        local bat_pct="${battery%%%(*}"
        if [[ "$bat_pct" =~ ^[0-9]+$ ]] && [[ "$bat_pct" -le 10 ]]; then
            bat_ind="[ERROR]"
        elif [[ "$bat_pct" =~ ^[0-9]+$ ]] && [[ "$bat_pct" -le 25 ]]; then
            bat_ind="[WARNING]"
        fi
        if [[ "$battery" == *"health:"* ]]; then
            local health_part="${battery##*health: }"
            local health_pct="${health_part%%%*}"
            if [[ "$health_pct" =~ ^[0-9]+$ ]] \
                && [[ "$health_pct" -le 50 ]]; then
                bat_ind="[ERROR]"
            elif [[ "$health_pct" =~ ^[0-9]+$ ]] \
                && [[ "$health_pct" -le 70 ]]; then
                bat_ind="[WARNING]"
            fi
        fi
        echo "Battery:            $bat_ind $battery"
    fi

    if [[ "$reboot_required" == "yes" ]]; then
        echo "Reboot Required:    [WARNING] yes"
    elif [[ "$reboot_required" == "no" ]]; then
        echo "Reboot Required:    [SUCCESS] no"
    else
        echo "Reboot Required:    $reboot_required"
    fi

    if [[ "$failed_units" == "none" ]]; then
        echo "Failed Units:       [SUCCESS] none"
    elif [[ "$failed_units" != "N/A" ]]; then
        echo "Failed Units:       [ERROR]"
        while IFS= read -r line; do
            echo "  $line"
        done <<< "$failed_units"
    else
        echo "Failed Units:       N/A"
    fi

    echo ""
    echo "--- Resource Usage ---"
    echo "CPU Load:  $load_avg"
    echo "Memory:    $memory"
    if [[ "$disk" == "N/A" || -z "$disk" ]]; then
        echo "Disks:     N/A"
    else
        echo "Disks:"
        echo "$disk" | sed 's/^/  /'
    fi

    if [[ "$node_ready" == "True" ]]; then
        echo ""
        echo "--- Pods ($pod_summary) ---"
        if [[ $total_pods -gt 0 ]]; then
            kubectl get pods --all-namespaces \
                --field-selector "spec.nodeName=$hostname" \
                --no-headers 2>/dev/null \
                | awk '{printf "  %-20s %-40s %s\n", $1, $2, $4}' \
                || true
        fi
    fi

    echo ""
}

if [[ "$JSON_OUTPUT" == "true" && "$TARGET_ALL" == "true" ]]; then
    json_results=()
    for i in "${!TARGET_NAMES[@]}"; do
        result=$(diagnose_node "${TARGET_NAMES[$i]}")
        json_results+=("$result")
    done
    printf '%s\n' "${json_results[@]}" | jq -s '.'
elif [[ "$JSON_OUTPUT" == "true" ]]; then
    diagnose_node "${TARGET_NAMES[0]}"
else
    for i in "${!TARGET_NAMES[@]}"; do
        diagnose_node "${TARGET_NAMES[$i]}"
    done

    if [[ "$TARGET_ALL" == "true" ]]; then
        echo "=== Cluster Summary ==="
        printf "%-20s %-8s %-8s %-10s %-8s %-8s %-8s %-8s %-8s\n" \
            "NODE" "ROLE" "READY" "SERVICE" "REBOOT" \
            "DRIVE" "MEM" "TEMP" "BATT"
        for i in "${!TARGET_NAMES[@]}"; do
            hostname="${TARGET_NAMES[$i]}"
            role=$(get_node_role "$hostname")

            ready=$(kubectl get node "$hostname" \
                -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' \
                2>/dev/null || echo "N/A")
            ready_short=$([[ "$ready" == "True" ]] \
                && echo "OK" || echo "FAIL")

            svc_name=$(get_k3s_service_name "$hostname")
            svc=$(run_on_node "$hostname" \
                "systemctl is-active '$svc_name'" \
                2>/dev/null || echo "N/A")
            svc_short=$([[ "$svc" == "active" ]] \
                && echo "OK" || echo "FAIL")

            reboot=$(check_reboot_required "$hostname")

            # Quick drive health check for summary
            drive_short=$(run_on_node_sudo "$hostname" "
                disks=\$(lsblk -dn -o NAME,TYPE | grep disk | awk '{print \$1}')
                if [ -z \"\$disks\" ]; then echo \"NONE\"; exit 0; fi
                failed=0
                for name in \$disks; do
                    dev=\"/dev/\$name\"
                    case \"\$name\" in
                        mmcblk*)
                            eol=\$(cat /sys/block/\$name/device/pre_eol_info 2>/dev/null)
                            case \"\$eol\" in
                                0x02|2|0x03|3) failed=1 ;;
                            esac
                            ;;
                        *)
                            command -v smartctl >/dev/null 2>&1 || continue
                            if smartctl -H \$dev 2>/dev/null | grep -E 'self-assessment|SMART Health Status' | grep -qv 'PASSED'; then
                                failed=1
                                break
                            fi
                            ;;
                    esac
                done
                dmesg | grep -qiE 'i/o error|buffer i/o error' && failed=1
                if [ \$failed -eq 1 ]; then echo \"FAIL\"; else echo \"OK\"; fi
            " 2>/dev/null || echo "N/A")

            temp_short=$(run_on_node "$hostname" "
                found=''
                for lf in /sys/class/hwmon/hwmon*/temp*_label; do
                    [ -f \"\$lf\" ] || continue
                    case \"\$(cat \"\$lf\" 2>/dev/null)\" in
                        'Package id'*)
                            v=\$(cat \"\${lf%_label}_input\" 2>/dev/null)
                            found=\"\$(( v / 1000 ))C\"; break ;;
                    esac
                done
                if [ -n \"\$found\" ]; then echo \"\$found\"
                elif [ -f /sys/class/thermal/thermal_zone0/temp ]; then
                    echo \"\$(( \$(cat /sys/class/thermal/thermal_zone0/temp) / 1000 ))C\"
                else echo 'N/A'; fi
            " 2>/dev/null || echo "N/A")

            mem_short=$(run_on_node "$hostname" "
                errs=0
                for mc in /sys/devices/system/edac/mc/mc*; do
                    [ -d \"\$mc\" ] || continue
                    ce=\$(cat \"\$mc/ce_count\" 2>/dev/null || echo 0)
                    ue=\$(cat \"\$mc/ue_count\" 2>/dev/null || echo 0)
                    errs=\$((errs + ce + ue))
                done
                hw=\$(dmesg 2>/dev/null \
                    | grep -ciE \
                    'hardware error|memory.*error|mce:.*bank|corrected error' \
                    || echo 0)
                errs=\$((errs + hw))
                if [ \"\$errs\" -gt 0 ]; then
                    echo \"FAIL\"
                else
                    echo \"OK\"
                fi
            " 2>/dev/null || echo "N/A")

            batt_short=$(run_on_node "$hostname" "
                if [ -d /sys/class/power_supply/BAT0 ]; then
                    cap=\$(cat /sys/class/power_supply/BAT0/capacity 2>/dev/null || echo '?')
                    echo \"\${cap}%\"
                elif [ -d /sys/class/power_supply/BAT1 ]; then
                    cap=\$(cat /sys/class/power_supply/BAT1/capacity 2>/dev/null || echo '?')
                    echo \"\${cap}%\"
                else
                    echo 'N/A'
                fi
            " 2>/dev/null || echo "N/A")

            printf "%-20s %-8s %-8s %-10s %-8s %-8s %-8s %-8s %-8s\n" \
                "$hostname" "$role" "$ready_short" \
                "$svc_short" "$reboot" "$drive_short" \
                "$mem_short" "$temp_short" "$batt_short"
        done
    fi
fi
