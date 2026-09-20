package ai.easyagent.mobile
import android.app.Activity
import android.os.Bundle
import android.webkit.*
import android.widget.*
import android.net.Uri
import org.json.JSONObject
import java.io.File

object Core {
    init { System.loadLibrary("easyagent_core") }
    @JvmStatic external fun dispatch(input: String): String
    fun checkpoint(context: android.content.Context, request: JSONObject): JSONObject {
        val result = JSONObject(dispatch(request.toString()))
        if (result.has("state")) {
            val file = android.util.AtomicFile(File(context.filesDir,"workflow-state.json"))
            val stream = file.startWrite()
            try {stream.write(result.getJSONObject("state").toString().toByteArray());file.finishWrite(stream)} catch(e:Exception){file.failWrite(stream);throw e}
        }
        return result
    }
}
class MainActivity: Activity() {
    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        val layout=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL}
        val address=EditText(this).apply {hint="https://你的 Hub 地址";setSingleLine();inputType=android.text.InputType.TYPE_TEXT_VARIATION_URI}
        val preferences=getSharedPreferences("hub",MODE_PRIVATE)
        address.setText(preferences.getString("url",""))
        val web=WebView(this)
        web.settings.javaScriptEnabled=true;web.settings.domStorageEnabled=true
        web.settings.allowFileAccess=false;web.settings.allowContentAccess=false
        web.settings.mixedContentMode=WebSettings.MIXED_CONTENT_NEVER_ALLOW
        web.webViewClient=object:WebViewClient(){override fun shouldOverrideUrlLoading(v:WebView,r:WebResourceRequest):Boolean = r.url.scheme!="https" || r.url.host!=Uri.parse(address.text.toString()).host}
        val button=Button(this).apply {text="连接工作室";setOnClickListener {val url=Uri.parse(address.text.toString());if(url.scheme=="https"&&url.host!=null&&url.userInfo==null){preferences.edit().putString("url",url.toString()).apply();web.loadUrl(url.toString())}}}
        val offline=Button(this).apply {text="运行本机离线流程";setOnClickListener {
            try {
                var result=Core.checkpoint(this@MainActivity,JSONObject("""{"op":"create","run_id":"${java.util.UUID.randomUUID()}","workflow":{"steps":[{"id":"material","kind":"transform","input":{"text":"确认搬家日期与纸箱数量"}},{"id":"receipt","target":"core.echo","depends_on":["material"],"input":{"text":{"${'$'}ref":"material.text"}}}]},"grants":{"core.echo":"read"}}"""))
                repeat(2){result=Core.checkpoint(this@MainActivity,JSONObject().put("op","next").put("state",result.getJSONObject("state")))}
                val actions=result.getJSONArray("actions")
                for(index in 0 until actions.length()){val a=actions.getJSONObject(index);result=Core.checkpoint(this@MainActivity,JSONObject().put("op","apply").put("state",result.getJSONObject("state")).put("event",JSONObject().put("type","complete").put("step_id",a.getString("step_id")).put("invocation_id",a.getString("invocation_id")).put("output",a.getJSONObject("input"))))}
                android.app.AlertDialog.Builder(this@MainActivity).setTitle("离线流程已保存").setMessage(result.toString(2)).setPositiveButton("完成",null).show()
            }catch(e:Throwable){android.app.AlertDialog.Builder(this@MainActivity).setTitle("本机执行失败").setMessage(e.message).setPositiveButton("关闭",null).show()}
        }}
        layout.addView(address);layout.addView(button);layout.addView(offline);layout.addView(web,LinearLayout.LayoutParams(-1,0,1f));setContentView(layout)
    }
}
