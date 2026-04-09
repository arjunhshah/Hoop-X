class TestWebSocket {
  constructor() {
    this.ws = null;
    this.port_range = [61863, 61873];
    this.port = 0; // 后续会随唤起网页时附带
    this.sid = "RRRR1111"; // 测试用id, 后续会随唤起网页时附带
    this.handlers = {};
    this.register_handler("hello_server", this._hello_client);
    this.register_handler("fetch_task_return", this._fetch_task_return);
    this.register_handler("web_connect_return", this._web_connect_return);
  }
  try_connect(port) {
    try {
      this.ws = new WebSocket(`ws://localhost:${port}/id=${this.sid}`);
      console.log("Connected to Rodin on port " + port);
      this.port = port;
      return true;
    } catch (e) {
      console.log("Port " + port + " is not available");
      return false;
    }
  }
  run() {
    for (var i = this.port_range[0]; i <= this.port_range[1]; i++)
      if (this.try_connect(i)) break;
    this.listen();
  }
  listen() {
    if (!this.ws) {
      console.log("No available port found");
      return;
    }
    this.ws.addEventListener("open", () => {
      console.log("Connected to Rodin");
    });
    this.ws.addEventListener("close", function (event) {
      console.log("Disconnected from Rodin");
      this.ws.close();
    });
    this.ws.addEventListener("message", ({ data }) => {
      try {
        var event = JSON.parse(data);
        this.handle(event);
      } catch (e) {
        console.log("Error parsing message: " + e);
      }
    });
  }
  handle(event) {
    if (this.handlers[event.type]) this.handlers[event.type](event);
    else
      throw new Error(
        `Unsupported event type: ${event.type} => ${JSON.stringify(event)}.`
      );
  }
  register_handler(event, handler) {
    this.handlers[event] = handler;
  }
  unregister_handler(event) {
    delete this.handlers[event];
  }
  _hello_client(event) {
    console.log("Received hello from Rodin: ", event.data);
  }
  _fetch_task_return(event) {
    console.log("Received fetch task from Rodin: ", event.data);
  }
  _web_connect_return(event) {
    console.log("Received web_connect_return from Rodin: ", event.data);
  }
  // 所有测试
  test_all() {
    this.echo_test();
    this.send_model_test();
    this.fetch_task_test();
    // this.fail_task_test();
    this.send_error_test();
  }
  // echo测试用
  echo_test() {
    this.send({
      type: "hello_client",
      data: "hello from web client",
    });
  }
  // 用于测试当前端口是否对应blender插件
  auth_test() {
    this.send({
      type: "web_connect",
      data: "hello from web client",
    });
  }
  // 下载模型时主动发送给blender
  send_model_test() {
    this.send({
      type: "send_model",
      data: {
        request_id: "id",
        sid: this.sid,
        files: {
          pbr: [
            {
              filename: "base.obj",
              format: "obj",
              length: 0,
              md5: "d6763e81896212e78d314f9c783c118a",
              content:
                "data:model/glb;base64,Z2xURgIAAADcAwAAGAMAAEpTT057ImFzc2V0Ijp7ImdlbmVyYXRvciI6Iktocm9ub3MgZ2xURiBC\nbGVuZGVyIEkvTyB2NC4zLjQ3IiwidmVyc2lvbiI6IjIuMCJ9LCJzY2VuZSI6MCwic2NlbmVzIjpb\neyJuYW1lIjoiU2NlbmUiLCJub2RlcyI6WzBdfV0sIm5vZGVzIjpbeyJtZXNoIjowLCJuYW1lIjoi\nQ3ViZSJ9XSwibWF0ZXJpYWxzIjpbeyJkb3VibGVTaWRlZCI6dHJ1ZSwibmFtZSI6Ik1hdGVyaWFs\nIiwicGJyTWV0YWxsaWNSb3VnaG5lc3MiOnsiYmFzZUNvbG9yRmFjdG9yIjpbMC44MDAwMDAwMTE5\nMjA5MjksMC44MDAwMDAwMTE5MjA5MjksMC44MDAwMDAwMTE5MjA5MjksMV0sIm1ldGFsbGljRmFj\ndG9yIjowLCJyb3VnaG5lc3NGYWN0b3IiOjAuNX19XSwibWVzaGVzIjpbeyJuYW1lIjoiQ3ViZSIs\nInByaW1pdGl2ZXMiOlt7ImF0dHJpYnV0ZXMiOnsiUE9TSVRJT04iOjB9LCJpbmRpY2VzIjoxLCJt\nYXRlcmlhbCI6MH1dfV0sImFjY2Vzc29ycyI6W3siYnVmZmVyVmlldyI6MCwiY29tcG9uZW50VHlw\nZSI6NTEyNiwiY291bnQiOjgsIm1heCI6WzEsMSwxXSwibWluIjpbLTEsLTEsLTFdLCJ0eXBlIjoi\nVkVDMyJ9LHsiYnVmZmVyVmlldyI6MSwiY29tcG9uZW50VHlwZSI6NTEyMywiY291bnQiOjM2LCJ0\neXBlIjoiU0NBTEFSIn1dLCJidWZmZXJWaWV3cyI6W3siYnVmZmVyIjowLCJieXRlTGVuZ3RoIjo5\nNiwiYnl0ZU9mZnNldCI6MCwidGFyZ2V0IjozNDk2Mn0seyJidWZmZXIiOjAsImJ5dGVMZW5ndGgi\nOjcyLCJieXRlT2Zmc2V0Ijo5NiwidGFyZ2V0IjozNDk2M31dLCJidWZmZXJzIjpbeyJieXRlTGVu\nZ3RoIjoxNjh9XX0gICCoAAAAQklOAAAAgD8AAIA/AACAvwAAgD8AAIC/AACAvwAAgD8AAIA/AACA\nPwAAgD8AAIC/AACAPwAAgL8AAIA/AACAvwAAgL8AAIC/AACAvwAAgL8AAIA/AACAPwAAgL8AAIC/\nAACAPwAABAAGAAAABgACAAMAAgAGAAMABgAHAAcABgAEAAcABAAFAAUAAQADAAUAAwAHAAEAAAAC\nAAEAAgADAAUABAAAAAUAAAABAA==\n", // base64 encoded
            },
            {
              filename: "texture_diffuse.png",
              format: "png",
              length: 0,
              md5: "",
              content: "data:image/png;base64,xxx",
            },
          ],
          shaded: [
            {
              filename: "base.obj",
              format: "obj",
              length: 0,
              md5: "",
              content: "data:image/png;base64,xxx",
            },
          ],
        },
      },
    });
  }
  // 从blender端获取task
  fetch_task_test() {
    this.send({
      type: "fetch_task",
      sid: this.sid,
    });
  }
  // 生成任务失败测试
  fail_task_test() {
    this.send({
      type: "fail_task",
      sid: this.sid,
      data: {
        Model: "Test",
      },
    });
  }
  // 错误测试
  send_error_test() {
    this.send({ type: "unknown_event" }); // 未能识别的事件
    this.send({}); // 空字典
    this.send("abcd"); // 字符串
    this.send(""); // 空字符串
    this.send(1); // 数字
    this.send([]); // 空列表
    this.send(); // 空
  }
  // 简单发送包装
  send(event) {
    this.ws.send(JSON.stringify(event));
  }
  // 关闭连接
  close() {
    this.ws.close();
  }
}

var test_ws = new TestWebSocket();
test_ws.run();
// 等待0.1秒
setTimeout(() => {
  test_ws.test_all();
}, 100);
