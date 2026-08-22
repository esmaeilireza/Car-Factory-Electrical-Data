"""
Basic test for the local model
"""
from llama_cpp import Llama
import time
import os

def test_model_loading():
    """Test model loading"""
    model_path = "models/qwen2.5-coder-1.5b-instruct-q6_k.gguf"
    
    # Check if file exists
    if not os.path.exists(model_path):
        print(f"❌ Model not found: {model_path}")
        print("Please download the model from this address:")
        print("https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF")
        return False
    
    print(f"📁 Model file size: {os.path.getsize(model_path) / (1024*1024):.2f} MB")
    
    # Test loading
    print("🔄 Loading model...")
    start_time = time.time()
    
    try:
        llm = Llama(
            model_path=model_path,
            n_ctx=4096,
            n_threads=4,
            verbose=True  # To see loading details
        )
        load_time = time.time() - start_time
        print(f"✅ Model loaded successfully in {load_time:.2f} seconds")
        return llm
    except Exception as e:
        print(f"❌ Error loading model: {e}")
        return None


def test_basic_inference(llm):
    """Test basic response generation"""
    print("\n🧪 Testing basic response...")
    
    test_prompts = [
        "What is Modbus TCP?",
        "What is a SCADA system?",
        "What does ANSI 49 protection mean?"
    ]
    
    for prompt in test_prompts:
        print(f"\n📝 Question: {prompt}")
        start_time = time.time()
        
        try:
            response = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": "You are an industrial automation expert."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=100,
                temperature=0.5
            )
            
            answer = response['choices'][0]['message']['content']
            response_time = time.time() - start_time
            
            print(f"✅ Answer ({response_time:.2f}s): {answer[:100]}...")
            
        except Exception as e:
            print(f"❌ Error in response: {e}")


if __name__ == "__main__":
    print("=" * 50)
    print("🧪 Testing Local Model Qwen2.5-Coder-1.5B")
    print("=" * 50)
    
    llm = test_model_loading()
    
    if llm:
        test_basic_inference(llm)
        print("\n✅ Tests completed successfully!")
    else:
        print("\n❌ Tests failed")