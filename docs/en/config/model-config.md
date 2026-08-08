
# 配置自定义的模型参数

请在 WebUI 的模型配置中编辑 `custom_extra_body`，或手动修改位于
`data/cmd_config.json` 下的配置文件。

找到 `provider`，并找到你想要修改的提供商的模型配置：

![alt text](https://files.astrbot.app/docs/source/images/model-config/image-2.png)

然后在模型的 `custom_extra_body` 中添加新的参数即可。Responses API 模型还可以按
模型配置原生工具，例如：

```json
{
  "custom_extra_body": {
    "tools": [
      {"type": "web_search"}
    ]
  }
}
```

原生工具由模型服务端执行；AstrBot 自身注册的函数工具仍由 AstrBot 的工具系统执行。

具体的参数请参看对应的提供商的文档。
