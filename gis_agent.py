# -*- coding: utf-8 -*-
"""QGIS telecom design agent runtime with local PyQGIS tools."""

import inspect
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor


class DictFunction:
    """Attribute wrapper for function-call dictionaries returned by urllib."""

    def __init__(self, data):
        self.name = data.get("name", "")
        self.arguments = data.get("arguments", "{}")


class DictToolCall:
    """Attribute wrapper for tool-call dictionaries returned by urllib."""

    def __init__(self, data):
        self.function = DictFunction(data.get("function", {}))


class DictMessage:
    """Small compatibility object matching OpenAI SDK message attributes."""

    def __init__(self, data):
        self.content = data.get("content")
        self.tool_calls = [
            DictToolCall(tool_call)
            for tool_call in data.get("tool_calls", []) or []
        ]


class GISToolkit:
    """PyQGIS tools exposed to the LLM agent."""

    def __init__(self, iface):
        self.iface = iface
        self.progress_callback = None

    def set_progress_callback(self, callback):
        self.progress_callback = callback

    def _emit(self, message):
        if self.progress_callback is not None:
            self.progress_callback(message)

    def create_telecom_layer(self, layer_name: str, layer_type: str):
        """
        Create one telecom planning layer in QGIS.
        :param layer_name: Layer name, such as 5G base stations, cable routes, or equipment rooms.
        :param layer_type: Geometry type. Must be Point or LineString.
        """
        self._emit("Preparing memory layer: {} ({})".format(layer_name, layer_type))
        geometry_type = "Point" if layer_type == "Point" else "LineString"
        layer = self._new_layer(layer_name, geometry_type)
        self._emit("Applying standard telecom style.")
        self._apply_style(layer, "base_station" if geometry_type == "Point" else "pipeline")
        QgsProject.instance().addMapLayer(layer)
        self._emit("Layer added to current QGIS project.")
        return "Created layer: {} ({})".format(layer_name, geometry_type)

    def diagnose_python_environment(self):
        """
        Diagnose the Python environment used by the running QGIS plugin.
        """
        try:
            import openai  # pylint: disable=import-outside-toplevel
            openai_status = "openai import ok: {}".format(
                getattr(openai, "__version__", "unknown version")
            )
        except Exception as exc:  # pylint: disable=broad-except
            openai_status = "openai import failed: {}".format(exc)

        site_paths = [path for path in sys.path if "site-packages" in path.lower()]
        return (
            "Python executable: {}\n"
            "Python version: {}\n"
            "{}\n"
            "Site packages: {}"
        ).format(
            sys.executable,
            sys.version.replace("\n", " "),
            openai_status,
            "; ".join(site_paths[:5]) or "not found in sys.path",
        )

    def create_standard_telecom_layers(self, project_name: str = "TelecomPlan"):
        """
        Create standard telecom drawing layers for aided design.
        :param project_name: Prefix used for generated standard layers.
        """
        layers = [
            ("{}_BaseStations".format(project_name), "Point", "base_station"),
            ("{}_EquipmentRooms".format(project_name), "Point", "equipment_room"),
            ("{}_Pipelines".format(project_name), "LineString", "pipeline"),
        ]
        created = []
        self._emit("Creating standard telecom drawing layers.")
        for name, geometry_type, facility_type in layers:
            self._emit("Creating layer: {}".format(name))
            layer = self._new_layer(name, geometry_type)
            self._apply_style(layer, facility_type)
            QgsProject.instance().addMapLayer(layer)
            created.append(name)
        self._emit("Standard layer template is ready.")
        return "Standard telecom layers created: {}".format(", ".join(created))

    def generate_parametric_telecom_plan(
        self,
        project_name: str = "DemoPlan",
        base_station_count: str = "12",
        equipment_room_count: str = "3",
        coverage_radius_m: str = "500",
    ):
        """
        Generate a telecom planning scene from parameters, then build nearest-node topology.
        :param project_name: Planning scene name.
        :param base_station_count: Number of base station points to generate.
        :param equipment_room_count: Number of equipment room points to generate.
        :param coverage_radius_m: Coverage radius stored as an attribute for each base station.
        """
        base_count = self._to_int(base_station_count, 12, minimum=1, maximum=500)
        room_count = self._to_int(equipment_room_count, 3, minimum=1, maximum=100)
        radius = self._to_float(coverage_radius_m, 500.0)
        self._emit(
            "Parsed parameters: project={}, base stations={}, equipment rooms={}, radius={}m."
            .format(project_name, base_count, room_count, radius)
        )

        base_layer = self._new_layer("{}_BaseStations".format(project_name), "Point")
        room_layer = self._new_layer("{}_EquipmentRooms".format(project_name), "Point")
        self._apply_style(base_layer, "base_station")
        self._apply_style(room_layer, "equipment_room")

        self._emit("Generating parametric facility points in current map extent.")
        extent = self._working_extent()
        base_points = self._grid_points(extent, base_count, margin_ratio=0.12)
        room_points = self._grid_points(extent, room_count, margin_ratio=0.28)
        self._add_point_features(base_layer, base_points, "BS", "base_station", radius)
        self._add_point_features(room_layer, room_points, "ER", "equipment_room", 0)

        self._emit("Adding base station and equipment room layers.")
        QgsProject.instance().addMapLayer(base_layer)
        QgsProject.instance().addMapLayer(room_layer)
        self._emit("Starting nearest-node topology design.")
        connect_result = self.auto_connect_points(base_layer.name(), room_layer.name())
        return (
            "Parametric telecom scene generated: {} base stations, {} equipment rooms. {}"
            .format(base_count, room_count, connect_result)
        )

    def auto_connect_points(
        self,
        source_layer_name: str,
        target_layer_name: str,
        output_layer_name: str = "",
    ):
        """
        Connect all source points to nearest target points and create a pipeline topology layer.
        :param source_layer_name: Base station/user point layer name.
        :param target_layer_name: Equipment room/aggregation node layer name.
        :param output_layer_name: Optional output pipeline layer name.
        """
        self._emit("Searching source and target point layers.")
        source_layer = self._find_layer(source_layer_name)
        target_layer = self._find_layer(target_layer_name)
        if source_layer is None:
            return "Source layer not found: {}".format(source_layer_name)
        if target_layer is None:
            return "Target layer not found: {}".format(target_layer_name)
        if (
            source_layer.geometryType() != QgsWkbTypes.PointGeometry
            or target_layer.geometryType() != QgsWkbTypes.PointGeometry
        ):
            return "Auto topology needs two point layers."

        target_features = list(target_layer.getFeatures())
        source_features = list(source_layer.getFeatures())
        if not target_features:
            return "Target layer has no point features: {}".format(target_layer_name)
        if not source_features:
            return "Source layer has no point features: {}".format(source_layer_name)

        self._emit("Building spatial index for {} target nodes.".format(len(target_features)))
        index = QgsSpatialIndex()
        for target_feature in target_features:
            index.addFeature(target_feature)
        target_by_id = {feature.id(): feature for feature in target_features}
        crs = source_layer.crs().authid() or "EPSG:4326"
        output_name = output_layer_name or "{}_To_{}_Pipelines".format(
            source_layer.name(),
            target_layer.name(),
        )
        output_layer = QgsVectorLayer("LineString?crs={}".format(crs), output_name, "memory")
        provider = output_layer.dataProvider()
        provider.addAttributes([
            QgsField("source_id", QVariant.Int),
            QgsField("target_id", QVariant.Int),
            QgsField("distance", QVariant.Double),
            QgsField("cable_type", QVariant.String),
        ])
        output_layer.updateFields()

        new_features = []
        self._emit("Connecting {} source points to nearest target nodes.".format(len(source_features)))
        for source in source_features:
            source_geom = source.geometry()
            if source_geom is None or source_geom.isEmpty():
                continue
            source_point = source_geom.asPoint()
            nearest_ids = index.nearestNeighbor(source_point, 1)
            if not nearest_ids:
                continue
            target = target_by_id[nearest_ids[0]]
            target_point = target.geometry().asPoint()

            feature = QgsFeature(output_layer.fields())
            feature.setGeometry(
                QgsGeometry.fromPolylineXY([
                    QgsPointXY(source_point),
                    QgsPointXY(target_point),
                ])
            )
            feature.setAttributes([
                int(source.id()),
                int(target.id()),
                float(source_geom.distance(target.geometry())),
                "fiber",
            ])
            new_features.append(feature)

        if not new_features:
            return "No pipeline was generated. Please check geometries."

        self._emit("Writing {} pipeline features.".format(len(new_features)))
        provider.addFeatures(new_features)
        output_layer.updateExtents()
        self._apply_style(output_layer, "pipeline")
        QgsProject.instance().addMapLayer(output_layer)
        self._emit("Pipeline topology layer added to QGIS.")
        return "Generated {} nearest-node pipeline links: {}".format(
            len(new_features),
            output_name,
        )

    def _new_layer(self, layer_name, geometry_type):
        layer = QgsVectorLayer("{}?crs=EPSG:4326".format(geometry_type), layer_name, "memory")
        provider = layer.dataProvider()
        if geometry_type == "Point":
            provider.addAttributes([
                QgsField("code", QVariant.String),
                QgsField("facility", QVariant.String),
                QgsField("capacity", QVariant.Int),
                QgsField("radius_m", QVariant.Double),
                QgsField("remark", QVariant.String),
            ])
        else:
            provider.addAttributes([
                QgsField("code", QVariant.String),
                QgsField("facility", QVariant.String),
                QgsField("cable_type", QVariant.String),
                QgsField("remark", QVariant.String),
            ])
        layer.updateFields()
        return layer

    def _add_point_features(self, layer, points, code_prefix, facility_type, radius):
        provider = layer.dataProvider()
        features = []
        for index, point in enumerate(points, start=1):
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point[0], point[1])))
            feature.setAttributes([
                "{}{:03d}".format(code_prefix, index),
                facility_type,
                64 if facility_type == "base_station" else 512,
                float(radius),
                "AI generated",
            ])
            features.append(feature)
        provider.addFeatures(features)
        layer.updateExtents()

    def _apply_style(self, layer, facility_type):
        if layer.geometryType() == QgsWkbTypes.PointGeometry:
            color = QColor("#1976d2") if facility_type == "base_station" else QColor("#d32f2f")
            symbol = QgsMarkerSymbol.createSimple({
                "name": "circle",
                "color": color.name(),
                "outline_color": "#ffffff",
                "size": "4.0",
            })
            layer.renderer().setSymbol(symbol)
        elif layer.geometryType() == QgsWkbTypes.LineGeometry:
            symbol = QgsLineSymbol.createSimple({
                "color": "#2e7d32",
                "width": "0.8",
                "line_style": "solid",
            })
            layer.renderer().setSymbol(symbol)

    def _working_extent(self):
        try:
            canvas = self.iface.mapCanvas()
            extent = canvas.extent()
            if extent and extent.width() > 0 and extent.height() > 0:
                return (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())
        except Exception:  # pylint: disable=broad-except
            pass
        return (116.30, 39.85, 116.50, 40.02)

    def _grid_points(self, extent, count, margin_ratio):
        xmin, ymin, xmax, ymax = extent
        width = xmax - xmin
        height = ymax - ymin
        xmin += width * margin_ratio
        xmax -= width * margin_ratio
        ymin += height * margin_ratio
        ymax -= height * margin_ratio

        columns = max(1, int(math.ceil(math.sqrt(count))))
        rows = max(1, int(math.ceil(float(count) / columns)))
        points = []
        for index in range(count):
            row = index // columns
            column = index % columns
            x_ratio = (column + 0.5) / columns
            y_ratio = (row + 0.5) / rows
            points.append((xmin + (xmax - xmin) * x_ratio, ymin + (ymax - ymin) * y_ratio))
        return points

    def _find_layer(self, layer_name):
        for layer in QgsProject.instance().mapLayers().values():
            if layer.name() == layer_name:
                return layer
        return None

    def _to_int(self, value, default, minimum, maximum):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    def _to_float(self, value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


class GISAgent:
    """LLM decision layer plus a local fallback for offline demos."""

    def __init__(self, toolkit):
        self.toolkit = toolkit
        self.memory = []
        self.max_memory_messages = 10
        self.tools = {
            "create_telecom_layer": toolkit.create_telecom_layer,
            "diagnose_python_environment": toolkit.diagnose_python_environment,
            "create_standard_telecom_layers": toolkit.create_standard_telecom_layers,
            "generate_parametric_telecom_plan": toolkit.generate_parametric_telecom_plan,
            "auto_connect_points": toolkit.auto_connect_points,
        }

    def run(self, user_prompt, progress_callback=None):
        self.progress_callback = progress_callback
        self.toolkit.set_progress_callback(progress_callback)
        self._emit("Agent received command.")
        if self._should_clear_memory(user_prompt):
            self.memory = []
            self._emit("Conversation memory cleared.")
            return ["Conversation memory cleared."]
        message = self._ask_llm(user_prompt)
        if message is None:
            self._emit("LLM unavailable, using local rule fallback.")
            results = self._run_local_fallback(user_prompt)
            if not self._is_diagnostic_prompt(user_prompt):
                self._remember("user", user_prompt)
                self._remember("assistant", "\n".join(results))
            return results
        self._emit("LLM response received, dispatching selected tool.")
        results = self._execute_message(message)
        if not self._is_diagnostic_prompt(user_prompt):
            self._remember("user", user_prompt)
            self._remember("assistant", self._message_summary(message, results))
        return results

    def _emit(self, message):
        if getattr(self, "progress_callback", None) is not None:
            self.progress_callback(message)

    def tool_schemas(self, include_diagnostic=False):
        schemas = []
        for name, func in self.tools.items():
            if name == "diagnose_python_environment" and not include_diagnostic:
                continue
            schemas.append(self._function_schema(name, func))
        return schemas

    def _ask_llm(self, user_prompt):
        self._emit("Loading model configuration.")
        config = self._load_config()
        api_key = (
            config.get("api_key")
            or config.get("deepseek_api_key")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("AI_STUDIO_API_KEY")
        )
        if not api_key:
            self._emit("No API key found in config or environment.")
            return None

        base_url = (
            config.get("base_url")
            or os.environ.get("DEEPSEEK_BASE_URL")
            or os.environ.get("AI_STUDIO_BASE_URL")
            or "https://api.deepseek.com"
        )
        model = (
            config.get("model")
            or os.environ.get("DEEPSEEK_MODEL")
            or os.environ.get("AI_STUDIO_MODEL")
            or "deepseek-chat"
        )

        self._emit("Sending request to model: {}.".format(model))
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a telecom engineering GIS agent for QGIS. "
                    "Choose PyQGIS tools to finish communication facility planning tasks: "
                    "standard layer generation, parametric base-station/equipment-room scene "
                        "construction, and nearest-node pipeline topology design. "
                        "Use tool calls for map-changing operations. Reply in Chinese when no tool is needed."
                        " Never diagnose Python unless the latest user message explicitly asks for diagnostics."
                    ),
                },
        ] + self.memory + [{"role": "user", "content": user_prompt}]
        tools = self.tool_schemas(include_diagnostic=self._is_diagnostic_prompt(user_prompt))
        return self._chat_completion(api_key, base_url, model, messages, tools)

    def _chat_completion(self, api_key, base_url, model, messages, tools):
        try:
            from openai import OpenAI  # pylint: disable=import-outside-toplevel
        except ImportError:
            self._emit("OpenAI SDK not found, using urllib fallback.")
            return self._chat_completion_with_urllib(api_key, base_url, model, messages, tools)

        self._emit("Using OpenAI-compatible SDK client.")
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        return response.choices[0].message

    def _chat_completion_with_urllib(self, api_key, base_url, model, messages, tools):
        url = base_url.rstrip("/") + "/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": "Bearer {}".format(api_key),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("LLM HTTP {}: {}".format(exc.code, error_body))
        except urllib.error.URLError as exc:
            raise RuntimeError("LLM request failed: {}".format(exc))

        data = json.loads(body)
        message = data["choices"][0]["message"]
        return DictMessage(message)

    def _load_config(self):
        config_path = Path(__file__).with_name("config.json")
        if not config_path.exists():
            return {}
        try:
            with config_path.open("r", encoding="utf-8") as config_file:
                config = json.load(config_file)
        except (OSError, ValueError):
            return {}
        if not isinstance(config, dict):
            return {}
        return config

    def _execute_message(self, message):
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            content = getattr(message, "content", "")
            return [content or "Agent did not choose a tool."]

        results = []
        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function = self.tools.get(function_name)
            if function is None:
                results.append("Unknown tool: {}".format(function_name))
                continue
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
                self._emit("Executing tool: {} with {}.".format(function_name, arguments))
                results.append(function(**arguments))
            except Exception as exc:  # pylint: disable=broad-except
                results.append("{} failed: {}".format(function_name, exc))
        return results

    def _run_local_fallback(self, user_prompt):
        prompt = user_prompt.strip()
        if any(word in prompt for word in ("环境", "python", "Python", "openai", "诊断")):
            return [self.toolkit.diagnose_python_environment()]
        if any(word in prompt for word in ("标准", "图纸", "图层")):
            return [self.toolkit.create_standard_telecom_layers()]
        if any(word in prompt for word in ("场景", "参数", "一键", "规划", "生成")):
            project_name = self._extract_project_name(prompt)
            base_station_count = self._extract_count(prompt, "基站", "12")
            equipment_room_count = self._extract_count(prompt, "机房", "3")
            coverage_radius_m = self._extract_radius(prompt, "500")
            return [
                self.toolkit.generate_parametric_telecom_plan(
                    project_name=project_name,
                    base_station_count=base_station_count,
                    equipment_room_count=equipment_room_count,
                    coverage_radius_m=coverage_radius_m,
                )
            ]
        if any(word in prompt for word in ("连线", "连接", "最近", "拓扑")):
            layers = [
                layer.name()
                for layer in QgsProject.instance().mapLayers().values()
                if layer.geometryType() == QgsWkbTypes.PointGeometry
            ]
            if len(layers) >= 2:
                return [self.toolkit.auto_connect_points(layers[0], layers[1])]
            return ["Local mode needs at least two point layers for topology design."]
        if any(word in prompt for word in ("线", "管线", "光缆", "线路")):
            return [self.toolkit.create_telecom_layer("FiberPipelines", "LineString")]
        return [self.toolkit.create_telecom_layer("BaseStations", "Point")]

    def _extract_project_name(self, prompt):
        patterns = [
            r"项目名(?:叫|为|是)?([\u4e00-\u9fa5A-Za-z0-9_-]+)",
            r"项目名称(?:叫|为|是)?([\u4e00-\u9fa5A-Za-z0-9_-]+)",
            r"([\u4e00-\u9fa5A-Za-z0-9_-]+规划)",
        ]
        for pattern in patterns:
            match = re.search(pattern, prompt)
            if match:
                return match.group(1).strip("，。,. ")
        return "DemoPlan"

    def _extract_count(self, prompt, keyword, default):
        match = re.search(r"(\d+)\s*个?\s*{}".format(keyword), prompt)
        if match:
            return match.group(1)
        match = re.search(r"{}\s*(\d+)\s*个?".format(keyword), prompt)
        if match:
            return match.group(1)
        return default

    def _extract_radius(self, prompt, default):
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:米|m|M)", prompt)
        if match and "半径" in prompt:
            return match.group(1)
        return default

    def _remember(self, role, content):
        if not content:
            return
        self.memory.append({"role": role, "content": str(content)})
        if len(self.memory) > self.max_memory_messages:
            self.memory = self.memory[-self.max_memory_messages:]

    def _message_summary(self, message, results):
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            tool_names = [tool_call.function.name for tool_call in tool_calls]
            return "Called tools: {}. Results: {}".format(
                ", ".join(tool_names),
                "\n".join(results),
            )
        return getattr(message, "content", "") or "\n".join(results)

    def _should_clear_memory(self, user_prompt):
        return any(
            word in user_prompt
            for word in ("清空记忆", "清除记忆", "重置对话", "重新开始", "clear memory")
        )

    def _is_diagnostic_prompt(self, user_prompt):
        return any(
            word in user_prompt
            for word in ("环境", "python", "Python", "openai", "诊断")
        )

    def _function_schema(self, name, function):
        signature = inspect.signature(function)
        properties = {}
        required = []
        for param_name, parameter in signature.parameters.items():
            properties[param_name] = {
                "type": "string",
                "description": self._param_description(function, param_name),
            }
            if param_name == "layer_type":
                properties[param_name]["enum"] = ["Point", "LineString"]
            if parameter.default is inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": inspect.getdoc(function).splitlines()[0],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def _param_description(self, function, param_name):
        doc = inspect.getdoc(function) or ""
        token = ":param {}:".format(param_name)
        for line in doc.splitlines():
            line = line.strip()
            if line.startswith(token):
                return line.replace(token, "").strip()
        return param_name
