# Source before running a script by hand:  source env.sh
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here:$here/pipeline:$here/quant:$here/strategy:$here/signals:$here/tracking:$here/service:$here/research:$here/tools"
