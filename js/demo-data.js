/* PYROTECH VISION — catalogue definitions (detection modules, event types, camera types, defaults).
 * No demonstration or sample records: every camera, detection, alert and snapshot shown in the
 * console comes from the backend (or from cameras the operator configured). The file keeps its
 * original name so existing installs load it without changes. */
(function () {
  'use strict';

  const MODULES = [
    { id: 'helmet', name: 'Helmet Detection', long: 'Construction Helmet Detection', icon: 'helmet', color: '#FFB547',
      desc: 'Detect construction helmets and flag workers without one.', classes: ['helmet', 'no_helmet'] },
    { id: 'ppe', name: 'PPE Detection', long: 'PPE Detection', icon: 'vest', color: '#FF8A3D',
      desc: 'Per person: construction helmet (and colour), safety vest (and colour), gloves.', classes: ['Helmet missing', 'Road helmet (not construction)', 'Vest missing', 'Vest wrong colour', 'Gloves missing'], live: true },
    { id: 'fire', name: 'Fire & Smoke', long: 'Fire & Smoke Detection', icon: 'flame', color: '#FF4D5B',
      desc: 'Detect visible flame and smoke in the scene.', classes: ['Fire', 'Smoke'], live: true },
    { id: 'human', name: 'Human Detection', long: 'Human Detection', icon: 'user', color: '#3478FF',
      desc: 'Detect, track and count people and riders.', classes: ['Person', 'Rider', 'Person (far)'], live: true },
    { id: 'vehicle', name: 'Vehicle Detection', long: 'Vehicle Detection', icon: 'car', color: '#8057FF',
      desc: 'Detect and track 2-wheelers, 4-wheelers, buses and trucks.', classes: ['2-Wheeler', '4-Wheeler', 'Bus', 'Truck'], live: true },
    { id: 'count', name: 'Human Count', long: 'Human Count', icon: 'users', color: '#13D99A',
      desc: 'Count people live; each person is counted once.', classes: ['Person'], live: true },
    { id: 'twhelmet', name: 'Two-Wheeler Helmet (Line)', long: 'Two-Wheeler Helmet Detection with Line Crossing', icon: 'helmet', color: '#FFB547',
      desc: 'Draw a line; when a motorcycle / scooter crosses it, check its rider for a helmet.', classes: ['Helmet compliant', 'Helmet missing', 'Helmet unverified'], live: true },
    { id: 'pcross', name: 'Person Line Crossing', long: 'Person Line Crossing & Counting', icon: 'users', color: '#13D99A',
      desc: 'Draw a line; people crossing IN and OUT are tracked and counted once.', classes: ['Person IN', 'Person OUT'], live: true },
    { id: 'object', name: 'Object Monitoring', long: 'Object Monitoring', icon: 'box', color: '#2BB8FF',
      desc: 'Mark objects on the live picture; alert when one is moved or missing.', classes: ['Moved', 'Missing'], live: true },
    { id: 'custom', name: 'Custom Model', long: 'Custom AI Model', icon: 'model', color: '#B07CFF',
      desc: 'Run your own trained model from the registry.', classes: [] }
  ];

  const EVENT_TYPES = {
    helmet_ok:      { label: 'Helmet detected',  module: 'helmet', severity: 'low',      tone: 'success' },
    helmet_missing: { label: 'Helmet missing',   module: 'helmet', severity: 'high',     tone: 'danger' },
    ppe_incomplete: { label: 'PPE incomplete',   module: 'ppe',    severity: 'medium',   tone: 'warning' },
    ppe_missing:    { label: 'PPE missing',      module: 'ppe',    severity: 'high',     tone: 'danger' },
    ppe_vest_color: { label: 'Vest wrong colour', module: 'ppe',   severity: 'medium',   tone: 'warning' },
    ppe_review:     { label: 'PPE needs review', module: 'ppe',    severity: 'low',      tone: 'info' },
    twhelmet_compliant: { label: 'Two-wheeler helmet compliant', module: 'twhelmet', severity: 'low', tone: 'success' },
    twhelmet_missing:   { label: 'Two-wheeler helmet missing',   module: 'twhelmet', severity: 'high', tone: 'danger' },
    twhelmet_unverified:{ label: 'Two-wheeler helmet not verified', module: 'twhelmet', severity: 'low', tone: 'warning' },
    line_cross_in:  { label: 'Person crossed line (IN)',  module: 'pcross', severity: 'low', tone: 'info' },
    line_cross_out: { label: 'Person crossed line (OUT)', module: 'pcross', severity: 'low', tone: 'info' },
    fire:           { label: 'Fire detected',    module: 'fire',   severity: 'critical', tone: 'critical' },
    smoke:          { label: 'Smoke detected',   module: 'fire',   severity: 'high',     tone: 'danger' },
    person:         { label: 'Person detected',  module: 'human',  severity: 'low',      tone: 'info' },
    intrusion:      { label: 'Unauthorized access', module: 'human', severity: 'medium', tone: 'warning' },
    vehicle:        { label: 'Vehicle detected', module: 'vehicle', severity: 'low',     tone: 'purple' },
    rider:          { label: 'Rider detected',   module: 'human',  severity: 'low',      tone: 'info' },
    occupancy:      { label: 'People count threshold reached', module: 'count', severity: 'medium', tone: 'warning' },
    head_count:     { label: 'Head count',       module: 'count',  severity: 'low',      tone: 'success' },
    object_moved:   { label: 'Object moved',     module: 'object', severity: 'high',     tone: 'warning' },
    object_missing: { label: 'Object missing',   module: 'object', severity: 'high',     tone: 'danger' },
    object_restored:{ label: 'Object back in place', module: 'object', severity: 'low',  tone: 'success' },
    manual:         { label: 'Manual capture',   module: 'custom', severity: 'low',      tone: 'info' },
    camera_disconnected: { label: 'Camera disconnected', module: 'custom', severity: 'medium', tone: 'warning' },
    engine_failure: { label: 'Detection engine failure', module: 'custom', severity: 'high', tone: 'danger' },
    snapshot_failed: { label: 'Snapshot saving failed', module: 'custom', severity: 'medium', tone: 'warning' },
    object_missing: { label: 'Object missing',   module: 'object', severity: 'medium',   tone: 'warning' }
  };

  const CAMERA_TYPES = [
    { id: 'rtsp', name: 'RTSP IP Camera' },
    { id: 'onvif', name: 'ONVIF Camera' },
    { id: 'webcam', name: 'Webcam (USB / local device)' },
    { id: 'file', name: 'Video File / Test Source' }
  ];
  const BRANDS = ['Hikvision', 'Dahua', 'Axis', 'CP Plus', 'Honeywell', 'Bosch', 'Hanwha', 'Uniview', 'Other'];
  const LOCATIONS = ['Main Gate', 'Warehouse', 'Construction Area', 'Parking', 'Loading Area', 'Side View', 'Control Room', 'Store Room', 'Pump House'];
  const ZONE_COLORS = ['#3478FF', '#FF4D5B', '#13D99A', '#FFB547', '#8057FF', '#2BB8FF', '#FF8A3D', '#E85BD0'];

  function defaultDetection() {
    return {
      confidence: 0.5,
      sensitivity: 'medium',
      modules: {
        helmet: { enabled: false, confidence: 0.5, detectHelmet: true, detectMissing: true, alertOnMissing: true, zones: [] },
        // PPE mirrors the backend defaults (backend/detection/modules.py DEFAULTS["ppe"])
        ppe: { enabled: false, confidence: 0.45, required: { helmet: true, vest: true, gloves: true }, vestColor: 'orange', zones: [] },
        // Fire & Smoke / Object Monitoring mirror the backend defaults (backend/detection/modules.py)
        fire: { enabled: false, classes: { fire: true, smoke: true }, classConf: { Fire: 0.30, Smoke: 0.65 }, confidence: 0.50, alert: { severity: 'critical' }, zones: [] },
        // human / vehicle mirror the backend defaults (original human_detect_yolo-2.py values)
        human: { enabled: false, confidence: 0.35, classConf: { Person: 0.30 }, classes: { person: true, rider: true, personFar: true, animalFilter: true }, zones: [] },
        vehicle: { enabled: false, confidence: 0.35, classConf: { '2-Wheeler': 0.35, '4-Wheeler': 0.35, Bus: 0.55, Truck: 0.55 }, classes: { twoWheeler: true, fourWheeler: true, bus: true, truck: true }, zones: [] },
        // Human Count mirrors the backend defaults (backend/detection/modules.py)
        count: { enabled: false, confidence: 0.40, classConf: { Person: 0.40 }, classes: { person: true }, counting: { minFrames: 3, keepLostSec: 2.0 }, zones: [] },
        object: { enabled: false, confidence: 0.15, zones: [] },
        // line-crossing modules mirror the backend defaults (backend/detection/modules.py); the line is drawn on their pages
        twhelmet: { enabled: false, confidence: 0.35 },
        pcross: { enabled: false, confidence: 0.35 },
        custom: { enabled: false, confidence: 0.5, modelId: '', zones: [] }
      }
    };
  }

  function defaultRecording() {
    return { snapshotOnDetection: true, snapshotClasses: ['helmet_missing', 'ppe_incomplete', 'fire', 'smoke', 'intrusion'],
      continuous: false, preEventSec: 5, postEventSec: 10, retentionDays: 30, maxStorageGb: 50 };
  }

  function newCamera() {
    return {
      id: '', name: '', type: 'rtsp', sourceUrl: '', location: '', brand: '', model: '', username: '',
      description: '', streamProfile: 'main', customStream: '', aiStream: 'same', aiCustomStream: '', snapshotSource: 'main', timeoutSec: 10, reconnect: 'auto', reconnectIntervalSec: 15,
      enabled: true, status: 'unverified', lastConnectedAt: null, lastError: null,
      detection: defaultDetection(), zones: [], zonesEnabled: true, recording: defaultRecording(),
      createdAt: null, updatedAt: null
    };
  }

  window.AV.catalog = {
    MODULES: MODULES, EVENT_TYPES: EVENT_TYPES, CAMERA_TYPES: CAMERA_TYPES, BRANDS: BRANDS, LOCATIONS: LOCATIONS,
    ZONE_COLORS: ZONE_COLORS, defaultDetection: defaultDetection, defaultRecording: defaultRecording, newCamera: newCamera,
    moduleById: function (id) { return MODULES.find(function (m) { return m.id === id; }); }
  };
})();
