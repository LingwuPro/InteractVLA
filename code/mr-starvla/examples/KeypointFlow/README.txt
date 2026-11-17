Start the native inference service with a checkpoint, adapter manifest and platform configuration.

python -m starVLA.keypoint_flow.serve_starvla --checkpoint PATH --manifest ADAPTER_JSON --config PLATFORM_JSON
python -m starVLA.keypoint_flow.serve_pi05 --checkpoint PATH --manifest ADAPTER_JSON --config PLATFORM_JSON --openpi-config NAME

Platform configurations: libero.json, robodojo.json, robocasa365.json, dexmal.json.
The adapter contains the checkpoint normalization, channel layout, kinematics and controller calibration.
Training and readout artifacts are not service inputs.
